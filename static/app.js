"use strict";

const state = {
  players: [],
  accounts: null, // null = all tracked accounts; else array of selected puuids
  range: "all",
  from: null,
  to: null,
  champion: "",
  queue: "",
  side: "", // "" all | blue | red
  roleFilter: "mine", // "mine" (main+secondary) | "" (all) | a team_position
  mainRole: "",       // from settings
  secondaryRole: "",
  rankTier: "",
  minGames: 1,
  mainView: "overview", // overview | matchups | progress | trends | blocks | settings
  progressChampion: null, // null = not initialized yet (defaults to Gwen)
  progressQueue: "",
  progressSide: "",
  ddragonVersion: null,
  ddragonVersions: [], // recent DDragon versions, newest first (patch picker)
  poolOrder: null, // champion pool flattened in priority order; null = not fetched
  dateFormat: "iso", // iso | us | eu — drives fmtDate/fmtTime
  championByKey: null, // numeric championId -> champion id (live-game lookup)
};

const QUEUE_NAMES = { 400: "Normal Draft", 420: "Ranked Solo", 430: "Normal Blind",
                      440: "Ranked Flex", 490: "Quickplay", 700: "Clash" };
const DISPLAY_NAME_FIXES = { MonkeyKing: "Wukong", FiddleSticks: "Fiddlesticks" };

const $ = (sel) => document.querySelector(sel);

function selectedPuuids() {
  return state.accounts ?? state.players.map((p) => p.puuid);
}

// append the account scope to a query; no params = all tracked (server default)
function accountParams(params = new URLSearchParams()) {
  if (state.accounts) for (const p of state.accounts) params.append("puuid", p);
  return params;
}

function displayName(champ) { return DISPLAY_NAME_FIXES[champ] || champ; }

// match-v5 spellings that differ from the DDragon id used in icon URLs
const ICON_NAME_FIXES = { FiddleSticks: "Fiddlesticks" };

// numeric championId (from spectator/live-game) -> DDragon champion id, built
// from DDragon champion.json once and cached
async function loadChampionKeyMap() {
  if (state.championByKey) return state.championByKey;
  const map = {};
  try {
    const data = await getJSON(
      `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/data/en_US/champion.json`);
    for (const c of Object.values(data.data || {})) map[+c.key] = c.id;
  } catch { /* offline — live lookup can't map ids, handled by caller */ }
  state.championByKey = map;
  return map;
}

function champIcon(champ) {
  if (!state.ddragonVersion || !champ) return "";
  const id = ICON_NAME_FIXES[champ] || champ;
  const url = `https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/img/champion/${id}.png`;
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

// date/time rendering honours the user's date_format setting
// (state.dateFormat: "iso" YYYY-MM-DD / "us" M/D/YYYY / "eu" D/M/YYYY;
// iso & eu use 24h time, us uses 12h). Full year — blocks now show dates.
function fmtDate(ms) {
  const d = new Date(ms);
  const y = d.getFullYear(), m = d.getMonth() + 1, day = d.getDate();
  if (state.dateFormat === "us") return `${m}/${day}/${y}`;
  if (state.dateFormat === "eu") return `${day}/${m}/${y}`;
  return `${y}-${String(m).padStart(2, "0")}-${String(day).padStart(2, "0")}`; // iso (default)
}

function fmtTime(ms) {
  const d = new Date(ms);
  if (state.dateFormat === "us") {
    const h = d.getHours(), am = h < 12 ? "AM" : "PM", h12 = h % 12 || 12;
    return `${h12}:${String(d.getMinutes()).padStart(2, "0")} ${am}`;
  }
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function fmtDateTime(ms) {
  return `${fmtDate(ms)} ${fmtTime(ms)}`;
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

// "squared with vertical fill" — reads as table columns at any font size, and
// unlike an emoji it inherits the button's colour in both themes
const COLUMNS_ICON = "▥";

function colPrefs(storageKey, allKeys, defaultKeys = allKeys) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved)) return new Set(saved.filter((k) => allKeys.includes(k)));
  } catch { /* fall through to defaults */ }
  return new Set(defaultKeys);
}

// `defaultKeys` (optional) powers "Reset to defaults": it clears the saved
// preference so the view falls back to its built-in default set.
function renderColPicker(target, storageKey, columns, visible, onChange, defaultKeys) {
  const draw = () => {
    target.innerHTML = `<details class="col-picker">
      <summary class="preset icon-btn" title="Choose columns" aria-label="Choose columns"
        >${COLUMNS_ICON}</summary>
      <div class="col-menu">` + columns.map((c) =>
        `<label><input type="checkbox" data-col="${c.key}"
           ${visible.has(c.key) ? "checked" : ""}> ${c.label}</label>`).join("")
      + (defaultKeys
        ? `<button type="button" class="col-reset">Reset to defaults</button>` : "")
      + `</div></details>`;
    target.querySelectorAll("input").forEach((cb) =>
      cb.addEventListener("change", () => {
        cb.checked ? visible.add(cb.dataset.col) : visible.delete(cb.dataset.col);
        localStorage.setItem(storageKey, JSON.stringify([...visible]));
        onChange();
      }));
    const reset = target.querySelector(".col-reset");
    if (reset) {
      reset.addEventListener("click", () => {
        // mutate in place: some views (blocks) hold onto this Set
        visible.clear();
        defaultKeys.forEach((k) => visible.add(k));
        localStorage.removeItem(storageKey);
        const open = target.querySelector("details").open;
        draw();
        target.querySelector("details").open = open;  // keep the menu up
        onChange();
      });
    }
  };
  draw();
}

// ---------- shared table sorting ----------
// Column spec entry: {key, label, type: "num"|"text", get?(row), sortable?,
// cls?}. sortState is {key, dir} (dir 1 asc / -1 desc) held in a view's state.
// Rank tiers order low→high; UNKNOWN sorts last.
const RANK_ORDINAL = { IRON: 0, BRONZE: 1, SILVER: 2, GOLD: 3, PLATINUM: 4,
  EMERALD: 5, DIAMOND: 6, MASTER: 7, GRANDMASTER: 8, CHALLENGER: 9, UNKNOWN: -1 };

function defaultSortDir(col) {
  return col.type === "text" ? 1 : -1; // names A→Z, stats high→low
}

// a <thead> where sortable columns carry click-to-sort affordances.
// `leading`/`trailing` are raw <th> strings for non-data columns (toggles).
function sortableThead(columns, sortState, leading = "", trailing = "") {
  const cells = columns.map((c) => {
    if (c.sortable === false) return `<th class="${c.cls || ""}">${c.label}</th>`;
    const active = sortState.key === c.key;
    const arrow = active ? (sortState.dir === 1 ? "▲" : "▼") : "";
    const ariaSort = active ? (sortState.dir === 1 ? "ascending" : "descending") : "none";
    return `<th class="sortable ${c.cls || ""}${active ? " sorted" : ""}"
      data-sort="${c.key}" aria-sort="${ariaSort}" title="Sort by ${escapeHtml(c.label)}"
      >${c.label}<span class="sort-arrow">${arrow}</span></th>`;
  }).join("");
  return `<thead><tr>${leading}${cells}${trailing}</tr></thead>`;
}

function sortRows(rows, sortState, columns) {
  const col = columns.find((c) => c.key === sortState.key);
  if (!col) return rows.slice();
  const get = col.get || ((r) => r[col.key]);
  const dir = sortState.dir;
  return rows.slice().sort((a, b) => {
    const va = get(a), vb = get(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;   // nulls always last, regardless of direction
    if (vb == null) return -1;
    const cmp = (typeof va === "string" || typeof vb === "string")
      ? String(va).localeCompare(String(vb)) : va - vb;
    return dir * cmp;
  });
}

// wire click-to-sort on a rendered table; toggles direction on the active
// column, else switches to the clicked column at its natural default
function wireSortable(container, sortState, columns, rerender) {
  container.querySelectorAll("th[data-sort]").forEach((th) =>
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortState.key === key) {
        sortState.dir *= -1;
      } else {
        sortState.key = key;
        sortState.dir = defaultSortDir(columns.find((c) => c.key === key) || {});
      }
      rerender();
    }));
}

// team_position value -> label, in lane order
const ROLE_OPTS = [["TOP", "Top"], ["JUNGLE", "Jungle"], ["MIDDLE", "Mid"],
                   ["BOTTOM", "Bot"], ["UTILITY", "Support"]];
function roleSettingOptions(sel) { // Settings: None + each role
  return `<option value="">None</option>`
    + ROLE_OPTS.map(([v, l]) => `<option value="${v}" ${v === sel ? "selected" : ""}>${l}</option>`).join("");
}
function roleFilterOptions() { // filter rows: My roles / All / each role
  return `<option value="mine">My roles</option><option value="">All roles</option>`
    + ROLE_OPTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
}
// the team_position(s) the current role filter maps to ([] = all roles)
function roleParamList() {
  if (state.roleFilter === "mine") return [state.mainRole, state.secondaryRole].filter(Boolean);
  return state.roleFilter ? [state.roleFilter] : [];
}
function addRoleParams(params) { roleParamList().forEach((r) => params.append("role", r)); }
// keep all filter dropdowns showing the shared role filter
function syncRoleSelects() {
  ["#role-select", "#mu-role", "#trend-role"].forEach((id) => { const el = $(id); if (el) el.value = state.roleFilter; });
}
// apply main/secondary role from settings, default the filter, fill the dropdowns
function applyRoleSettings(settings) {
  state.mainRole = settings.main_role || "";
  state.secondaryRole = settings.secondary_role || "";
  state.roleFilter = state.mainRole ? "mine" : ""; // no main set -> show all roles
  ["#role-select", "#mu-role", "#trend-role"].forEach((id) => {
    const el = $(id);
    if (el && !el.options.length) el.innerHTML = roleFilterOptions();
  });
  syncRoleSelects();
}

function queryString() {
  const params = accountParams();
  if (state.range === "custom") {
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
  } else if (state.range !== "all") {
    params.set("range", state.range);
  }
  if (state.champion) params.set("champion", state.champion);
  if (state.queue) params.set("queue", state.queue);
  if (state.side) params.set("side", state.side);
  addRoleParams(params);
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
function metricGroupsPanel(data, storageKey) {
  if (data === undefined) return `<div class="muted">Loading…</div>`;
  if (data === null) return `<div class="muted">No detailed metrics recorded for this game.</div>`;
  const vis = storageKey ? visibleMetricKeys(storageKey) : null;
  const meta = vis ? data.meta.filter((m) => vis.has(m.key)) : data.meta;
  const groups = [...new Set(meta.map((m) => m.group))];
  return `<div class="metric-groups">` + groups.map((g) =>
    `<div class="metric-group"><h4>${g}</h4>` +
    meta.filter((m) => m.group === g).map((m) => `<div class="metric-row">
        <span class="metric-label">${m.label}</span>
        <span class="metric-value">${fmtMetric(data.metrics[m.key], m)}</span>
      </div>`).join("") + `</div>`).join("") + `</div>`;
}

function renderSummary(s) {
  const selected = state.players.filter((p) => selectedPuuids().includes(p.puuid));
  let rank;
  if (state.hideMyRank) {
    rank = "Hidden";
  } else if (selected.length === 1) {
    const p = selected[0];
    rank = p.solo_tier
      ? `${titleCase(p.solo_tier)} ${p.solo_division ?? ""} ${p.solo_lp ?? 0} LP`
      : "Unranked / unknown";
  } else {
    rank = selected.map((p) => `${escapeHtml(p.game_name)}: ${p.solo_tier
      ? fmtRank({ tier: p.solo_tier, division: p.solo_division, lp: p.solo_lp })
      : "–"}`).join("<br>") || "–";
  }
  $("#summary-tiles").innerHTML = `
    <div class="tile"><div class="label">Games</div><div class="value">${s.games}</div>
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
  let data = state.rankHistory;
  const target = $("#rank-chart");
  const legend = $("#rank-legend");
  $("#rank-section").classList.toggle("hidden", Boolean(state.hideMyRank));
  if (state.hideMyRank) return;
  if (data) data = { ...data, series: data.series.filter((s) => selectedPuuids().includes(s.puuid)) };
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

// Every column the summary already returns per champion — the table used to
// hard-code six of them; the rest are opt-in through the picker.
const CHAMP_ALL_COLS = [
  { key: "champion", label: "Champion", type: "text", get: (r) => displayName(r.champion),
    cell: (r) => `<td><span class="champ-cell">${champIcon(r.champion)}${
      displayName(r.champion)}</span></td>` },
  { key: "games", label: "Games", type: "num", cell: (r) => `<td>${r.games}</td>` },
  { key: "wins", label: "W–L", type: "num", get: (r) => r.wins,
    cell: (r) => `<td>${r.wins}–${r.games - r.wins}</td>` },
  { key: "winrate", label: "Winrate", type: "num", cls: "wr-col",
    cell: (r) => `<td class="wr-col">${wrCell(r.winrate)}</td>` },
  { key: "kda", label: "KDA", type: "num", cell: (r) => `<td>${fmt(r.kda, 2)}</td>` },
  { key: "kills", label: "Kills", type: "num", cell: (r) => `<td>${fmt(r.kills, 1)}</td>` },
  { key: "deaths", label: "Deaths", type: "num", cell: (r) => `<td>${fmt(r.deaths, 1)}</td>` },
  { key: "assists", label: "Assists", type: "num", cell: (r) => `<td>${fmt(r.assists, 1)}</td>` },
  { key: "cs_min", label: "CS/min", type: "num", cell: (r) => `<td>${fmt(r.cs_min)}</td>` },
  { key: "gold_min", label: "Gold/min", type: "num", cell: (r) => `<td>${fmt(r.gold_min, 0)}</td>` },
  { key: "dmg_min", label: "DMG/min", type: "num", cell: (r) => `<td>${fmt(r.dmg_min, 0)}</td>` },
  { key: "avg_duration_s", label: "Avg length", type: "num",
    cell: (r) => `<td>${fmtDuration(r.avg_duration_s)}</td>` },
];
const CHAMP_DEFAULT_COLS = ["champion", "games", "wins", "winrate", "kda", "cs_min"];
const champCols = colPrefs("cp-cols-champions", CHAMP_ALL_COLS.map((c) => c.key),
                           CHAMP_DEFAULT_COLS);
const champSort = { key: "games", dir: -1 };

function champVisibleCols() {
  // champion always leads — a row with no name can't be read
  return CHAMP_ALL_COLS.filter((c) => c.key === "champion" || champCols.has(c.key));
}

function renderChampionTable(byChampion) {
  const target = $("#champion-table");
  if (!byChampion.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games.</div></div>`;
    return;
  }
  state.byChampion = byChampion;
  const cols = champVisibleCols();
  // comparing a champion needs a champion, so the button lives per row
  const compare = state.enableComparison;
  const body = sortRows(byChampion, champSort, CHAMP_ALL_COLS).map((row) =>
    `<tr>${cols.map((c) => c.cell(row)).join("")}${compare
      ? `<td><button class="preset icon-btn champ-cmp-link" data-champ="${escapeHtml(row.champion)}"
           title="Compare this champion with other players"
           aria-label="Compare this champion with other players">⧉</button></td>` : ""}</tr>`).join("");
  target.innerHTML = `<div class="table-wrap"><table>
    ${sortableThead(cols, champSort, "", compare ? "<th></th>" : "")}
    <tbody>${body}</tbody></table></div>`;
  wireSortable(target, champSort, CHAMP_ALL_COLS, () => renderChampionTable(state.byChampion));
  target.querySelectorAll(".champ-cmp-link").forEach((btn) =>
    btn.addEventListener("click", () =>
      openComparison({ my: btn.dataset.champ, scope: "champion" })));
}

const recentUi = { runesOpen: new Set(), vodOpen: new Set(), sort: { key: "date", dir: -1 } };
// Full-game curve: no open-set of its own — it renders at the bottom of the
// VOD panel, so recentUi.vodOpen decides when it shows. Only the fetched data
// is held here, keyed per gkey and never cleared (recent games don't change
// once loaded for this view).
const curveUi = { cache: new Map() };

function kdaRatio(g) { return (g.kills + g.assists) / Math.max(1, g.deaths); }

// ---------- full-game gold/CS/XP/level curve chart ----------
// Gold and CS are the two most immediately useful reads for lane/game
// review; XP and level are available from the same endpoint if a future
// pass wants to add a metric switcher.
const GC_METRICS = [
  { key: "gold", label: "Gold", decimals: 0 },
  { key: "cs", label: "CS", decimals: 0 },
];
const GC_CHART_W = 260, GC_CHART_H = 100;
const GC_PAD = { l: 34, r: 8, t: 8, b: 18 };

function gcChartSVG(def, minutes, meValues, oppValues) {
  const mePts = minutes.map((m, i) => ({ x: m, v: meValues[i] })).filter((p) => p.v != null);
  const oppPts = oppValues
    ? minutes.map((m, i) => ({ x: m, v: oppValues[i] })).filter((p) => p.v != null)
    : [];
  const allVals = [...mePts.map((p) => p.v), ...oppPts.map((p) => p.v)];
  if (!allVals.length) return "";
  let lo = Math.min(...allVals), hi = Math.max(...allVals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  lo -= span * 0.08; hi += span * 0.08;
  const maxX = Math.max(...minutes, 1);
  const iw = GC_CHART_W - GC_PAD.l - GC_PAD.r, ih = GC_CHART_H - GC_PAD.t - GC_PAD.b;
  const x = (m) => GC_PAD.l + (m / maxX) * iw;
  const y = (v) => GC_PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
  const fmt = (v) => v.toFixed(def.decimals) + (def.suffix || "");
  const line = (pts, cls) => pts.length > 1
    ? `<polyline class="${cls}" points="${
        pts.map((p) => `${x(p.x).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")}"/>`
    : "";
  const maxV = Math.max(...allVals), minV = Math.min(...allVals);
  return `<figure class="trend-chart game-curve-chart">
    <figcaption>${def.label}</figcaption>
    <svg viewBox="0 0 ${GC_CHART_W} ${GC_CHART_H}" role="img"
         aria-label="${def.label} over the game">
      <line class="tl-axis" x1="${GC_PAD.l}" x2="${GC_CHART_W - GC_PAD.r}"
            y1="${GC_CHART_H - GC_PAD.b}" y2="${GC_CHART_H - GC_PAD.b}"/>
      <text class="tl-ylab" x="${GC_PAD.l - 4}" y="${y(maxV) + 3}" text-anchor="end">${fmt(maxV)}</text>
      <text class="tl-ylab" x="${GC_PAD.l - 4}" y="${y(minV) + 3}" text-anchor="end">${fmt(minV)}</text>
      ${line(mePts, "gc-line-me")}${line(oppPts, "gc-line-opp")}
      <text class="tl-xlab" x="${GC_PAD.l}" y="${GC_CHART_H - 4}">0m</text>
      <text class="tl-xlab" x="${GC_CHART_W - GC_PAD.r}" y="${GC_CHART_H - 4}" text-anchor="end">${maxX}m</text>
    </svg>
  </figure>`;
}

function gameCurveSection(gkey) {
  if (!curveUi.cache.has(gkey)) return `<p class="muted">Loading…</p>`;
  const curve = curveUi.cache.get(gkey);
  if (!curve) {
    return `<p class="muted">No full-game curve recorded — crawl again or run
      <code>./crawl.sh --backfill-frame-series</code>.</p>`;
  }
  const charts = GC_METRICS.map((def) =>
    gcChartSVG(def, curve.minutes, curve.me[def.key], curve.opp ? curve.opp[def.key] : null)
  ).join("");
  return `<div class="game-curve">
    <div class="game-curve-legend">
      <span class="gc-legend-me">● You</span>
      ${curve.opp ? `<span class="gc-legend-opp">● Opponent</span>` : ""}
    </div>
    <div class="chart-grid">${charts}</div>
  </div>`;
}

// fetched when the VOD panel opens — the curve lives at the bottom of it, so
// it has no toggle of its own
async function ensureGameCurve(gkey, matchId, myPuuid, oppPuuid) {
  if (curveUi.cache.has(gkey)) return;
  const params = new URLSearchParams({ match_id: matchId, puuid: myPuuid });
  if (oppPuuid) params.set("opp_puuid", oppPuuid);
  try {
    curveUi.cache.set(gkey, await getJSON(`/api/stats/game-curve?${params}`));
  } catch {
    curveUi.cache.set(gkey, null);
  }
}

function runesCompareCol(champ, runes, whose) {
  const body = runes
    ? `<div class="recent-runes-cell-inner">${
        runePageIcons(runes, { keystoneSize: 22, minorSize: 16, treeSize: 18, shardSize: 13 })}</div>`
    : `<p class="muted">Not recorded — crawl again or run
        <code>./crawl.sh --backfill-runes</code>.</p>`;
  return `<div class="runes-compare-col">
    <h5>${champIcon(champ)}${displayName(champ)} <span class="muted">(${whose})</span></h5>
    ${body}
  </div>`;
}

function renderRecent(recent) {
  const target = $("#recent-list");
  if (!recent.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games.</div></div>`;
    return;
  }
  const multi = selectedPuuids().length > 1;
  const colCount = 11 + (multi ? 1 : 0);
  const names = new Map(state.players.map((p) => [p.puuid, p.game_name]));
  const cols = [
    { key: "date", label: "Date", type: "num", get: (g) => g.game_creation_ms },
    ...(multi ? [{ key: "account", label: "Account", type: "text",
                   get: (g) => names.get(g.my_puuid) ?? "?" }] : []),
    { key: "queue", label: "Queue", type: "text", get: (g) => QUEUE_NAMES[g.queue_id] ?? String(g.queue_id) },
    { key: "me", label: "Me", type: "text", get: (g) => displayName(g.my_champion) },
    { key: "opponent", label: "Opponent", type: "text",
      get: (g) => (g.opp_champion ? displayName(g.opp_champion) : null) },
    { key: "opp_rank", label: "Opp. rank", type: "num",
      get: (g) => (g.opp_champion ? RANK_ORDINAL[g.rank_tier] : null) },
    { key: "result", label: "Result", type: "num", get: (g) => (g.win ? 1 : 0) },
    { key: "kda", label: "K/D/A", type: "num", get: kdaRatio },
    { key: "length", label: "Length", type: "num", get: (g) => g.game_duration_s },
    { key: "runes", label: "Runes", sortable: false },
    { key: "vod", label: "VOD", sortable: false },
    { key: "block", label: "", sortable: false },
  ];
  const body = sortRows(recent, recentUi.sort, cols).map((g) => {
    const gkey = `${g.match_id}:${g.my_puuid}`;
    const runesOpen = recentUi.runesOpen.has(gkey);
    const vodOpen = recentUi.vodOpen.has(gkey);
    const hasRunes = g.runes || g.opp_runes;
    const tagCount = reflectionTagCount(g.match_id, g.my_puuid);
    let html = `<tr>
      <td>${fmtDateTime(g.game_creation_ms)}</td>
      ${multi ? `<td>${escapeHtml(names.get(g.my_puuid) ?? "?")}</td>` : ""}
      <td>${QUEUE_NAMES[g.queue_id] ?? g.queue_id}</td>
      <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
      <td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>
      <td>${g.opp_champion ? titleCase(g.rank_tier) : "–"}</td>
      <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
      <td>${g.kills}/${g.deaths}/${g.assists}</td>
      <td>${fmtDuration(g.game_duration_s)}</td>
      <td>${hasRunes
        ? `<button class="preset seg-toggle runes-toggle" data-gkey="${gkey}"
             aria-expanded="${runesOpen}" title="Runes">${runesOpen ? "▾" : "▸"} Runes</button>`
        : `<span class="muted">–</span>`}</td>
      <td><button class="preset seg-toggle vod-toggle" data-gkey="${gkey}"
        data-match="${g.match_id}" data-puuid="${g.my_puuid}" data-opp="${g.opp_puuid ?? ""}"
        aria-expanded="${vodOpen}"
        title="Reflection, the recorded VOD with its map and chapters, and the full-game curve${
          tagCount ? ` — ${tagCount} reflection tag${tagCount === 1 ? "" : "s"}` : ""}"
        >${vodOpen ? "▾" : "▸"} 🎬 VOD${tagCount ? ` <span class="vod-tagcount">${tagCount}</span>` : ""}</button></td>
      <td><button class="preset promote-btn" data-match="${g.match_id}"
        data-puuid="${g.my_puuid}" title="Add to current block">+ Block</button></td>
    </tr>`;
    if (runesOpen) {
      html += `<tr class="games-row"><td colspan="${colCount}">
        <div class="runes-compare">${
          runesCompareCol(g.my_champion, g.runes, "you")}${
          g.opp_champion ? runesCompareCol(g.opp_champion, g.opp_runes, "opponent") : ""
        }</div>
      </td></tr>`;
    }
    // One panel per game, in the order you'd work through it: what you took
    // away from it, then the footage, then the shape of the whole game.
    // Reflection and the curve each used to be their own column; both are
    // about this single game, so a row of three toggles that open three
    // stacked panels was just three ways of saying "expand this game".
    // recordingSection() renders nothing when there is no recording, which is
    // right when it's tucked inside another panel but reads as a broken toggle
    // when it IS the panel — so say so explicitly here.
    if (vodOpen) {
      const rec = recordingSection(g.match_id, g.my_puuid);
      html += `<tr class="games-row"><td colspan="${colCount}">
        <div class="vod-reflect">${reflectionSection(g.match_id, g.my_puuid, g)}</div>
        ${rec || `<p class="muted">No recording found for this game. Ascent recordings are
            matched by game id — press 🎬 in the header to re-scan.</p>`}
        <div class="vod-curve"><h5>Full-game curve</h5>${gameCurveSection(gkey)}</div>
      </td></tr>`;
    }
    return html;
  }).join("");
  target.innerHTML = `<div class="table-wrap"><table>
    ${sortableThead(cols, recentUi.sort)}
    <tbody>${body}</tbody></table></div>`;
  wireSortable(target, recentUi.sort, cols, () => renderRecent(recent));
  wirePromoteButtons(target);
  target.querySelectorAll(".runes-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const gkey = btn.dataset.gkey;
      recentUi.runesOpen.has(gkey) ? recentUi.runesOpen.delete(gkey) : recentUi.runesOpen.add(gkey);
      renderRecent(recent);
    }));
  target.querySelectorAll(".vod-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const gkey = btn.dataset.gkey;
      if (recentUi.vodOpen.has(gkey)) {
        recentUi.vodOpen.delete(gkey);
        renderRecent(recent);
        return;
      }
      recentUi.vodOpen.add(gkey);
      renderRecent(recent); // show "Loading…" immediately
      await Promise.all([
        ensureReflection(btn.dataset.match, btn.dataset.puuid),
        ensureRecentRecording(btn.dataset.match, btn.dataset.puuid),
        ensureGameCurve(gkey, btn.dataset.match, btn.dataset.puuid, btn.dataset.opp),
      ]);
      renderRecent(recent);
    }));
  wireReflectionSection(target, async (matchId, puuid) => {
    reflectionUi.cache.delete(reflectionKey(matchId, puuid));
    await ensureReflection(matchId, puuid);
    renderRecent(recent);
  }, () => renderRecent(recent));
  wireRecordingSection(target, async (matchId, puuid) => {
    recordingUi.cache.delete(recordingKey(matchId, puuid));
    await ensureRecentRecording(matchId, puuid);
    renderRecent(recent);
  });
}

// loaded on demand when the 🎬 VOD toggle on a Recent games row is expanded
async function ensureRecentRecording(matchId, puuid) {
  const key = recordingKey(matchId, puuid);
  if (recordingUi.cache.has(key)) return;
  try {
    const data = await getJSON(
      `/api/recordings?match_id=${encodeURIComponent(matchId)}&puuid=${encodeURIComponent(puuid)}`);
    recordingUi.cache.set(key, data.recordings || []);
  } catch {
    recordingUi.cache.set(key, []);
  }
}

function wirePromoteButtons(container) {
  container.querySelectorAll(".promote-btn").forEach((btn) =>
    btn.addEventListener("click", () =>
      promoteGame(btn.dataset.match, btn.dataset.puuid, btn)));
}

function tierClass(player) {
  // no class when unranked or when the rank is hidden (server nulls solo_tier)
  return player && player.solo_tier ? ` tier-${player.solo_tier.toLowerCase()}` : "";
}

function renderAccountSelector() {
  const box = $("#account-select");
  box.classList.toggle("hidden", state.players.length < 2);
  if (state.players.length < 2) return;
  const btn = $("#account-select-btn");
  const selected = selectedPuuids();
  let label, cls = "";
  if (state.accounts === null) {
    label = "All accounts";
  } else if (selected.length === 1) {
    const p = state.players.find((q) => q.puuid === selected[0]);
    label = p ? p.game_name : "1 account";
    cls = tierClass(p);
  } else {
    label = `${selected.length} accounts`;
  }
  btn.innerHTML = `${escapeHtml(label)} ▾`;
  btn.className = `preset${cls}`;
  $("#account-select-menu").innerHTML =
    `<label><input type="checkbox" data-all ${state.accounts === null ? "checked" : ""}>
       All accounts</label>` +
    state.players.map((p) => `<label class="${tierClass(p).trim()}">
        <input type="checkbox" data-puuid="${p.puuid}"
          ${state.accounts !== null && state.accounts.includes(p.puuid) ? "checked" : ""}>
        ${escapeHtml(p.game_name)}#${escapeHtml(p.tag_line)}</label>`).join("");
  $("#account-select-menu").querySelectorAll("input").forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.dataset.all !== undefined) {
        state.accounts = null;
      } else {
        const checked = [...$("#account-select-menu")
          .querySelectorAll("input[data-puuid]:checked")].map((c) => c.dataset.puuid);
        // none or every account selected collapses back to "all"
        state.accounts = checked.length && checked.length < state.players.length
          ? checked : null;
      }
      accountSelectionChanged();
    }));
}

function accountSelectionChanged() {
  renderAccountSelector();
  // overview is always refreshed (it doesn't reload on nav); the active
  // stats view reloads its own data. Blocks/settings aren't account-scoped.
  loadFilterOptions().then(refresh);
  if (state.mainView === "matchups") initMatchups();
  else if (state.mainView === "progress") loadProgressFilterOptions().then(loadProgress);
  else if (state.mainView === "trends") initTrends(); // rebuilds filter options too
  else if (state.mainView === "guide") loadGuide(); // refresh matchup game counts for the new scope
}

// ---------- data loading ----------

async function loadFilterOptions() {
  const opts = await getJSON(`/api/filters?${accountParams()}`);
  // a filter value the new account scope can't produce silently zeroes all
  // stats while the dropdown shows "All" — reset instead
  if (state.champion && !opts.champions.includes(state.champion)) state.champion = "";
  if (state.queue && !opts.queues.map(String).includes(state.queue)) state.queue = "";
  if (state.rankTier && !opts.rank_tiers.includes(state.rankTier)) state.rankTier = "";
  $("#champion-select").innerHTML = `<option value="">All</option>` +
    opts.champions.map((c) => `<option value="${c}" ${c === state.champion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#queue-select").innerHTML = `<option value="">All</option>` +
    opts.queues.map((q) => `<option value="${q}" ${String(q) === state.queue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
  $("#rank-select").innerHTML = `<option value="">All</option>` +
    opts.rank_tiers.map((t) => `<option value="${t}" ${t === state.rankTier ? "selected" : ""}>${titleCase(t)}</option>`).join("");
}

let refreshSeq = 0;

async function refresh() {
  const seq = ++refreshSeq;
  const qs = queryString();
  const [summary, rankHistory] = await Promise.all([
    getJSON(`/api/stats/summary?${qs}`),
    getJSON("/api/stats/rank-history"),
  ]);
  if (seq !== refreshSeq) return; // superseded by a newer refresh
  state.rankHistory = rankHistory;
  renderSummary(summary);
  renderChampionTable(summary.by_champion ?? []);
  renderRecent(summary.recent ?? []);
  renderRankChart();
}

// ---------- coaching progress ----------

// The row's heading: the session's own title, or a stand-in built from its
// date. The bold line used to be the raw date range, which the line under it
// then repeated — so the title (the only thing that says what the session was
// about) was buried at the end of a muted subtitle.
function periodTitle(segment) {
  if (segment.session_title) return segment.session_title;
  if (segment.session_date) return `Coaching session, ${fmtDate(segment.from_ms)}`;
  return segment.label;            // Baseline — not a session at all
}

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

const segmentUi = { expanded: new Set(), expandedGames: new Set(), cache: new Map(),
                    vodOpen: new Set(), segments: [],
                    // display order only — deltas are always read against the
                    // chronologically previous period, whichever way it's shown
                    order: localStorage.getItem("cp-progress-order") || "asc" };

function segKey(segment) {
  return `${segment.from_ms}:${segment.to_ms}`;
}

function progressFilterParams(segment) {
  const params = accountParams(
    new URLSearchParams({ from_ms: segment.from_ms, to_ms: segment.to_ms - 1 }));
  if (state.progressChampion) params.set("champion", state.progressChampion);
  if (state.progressQueue) params.set("queue", state.progressQueue);
  if (state.progressSide) params.set("side", state.progressSide);
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
  if (value == null) return "–";
  const sign = m.signed && value > 0 ? "+" : "";
  return sign + value.toFixed(m.decimals) + (m.suffix || "");
}

// ---------- per-view metric column pickers ----------
// All metric panels render from the shared registry meta; each view keeps its
// own visible set (default_hidden metrics — e.g. the lane Δ's — start off).

async function ensureMetricsMeta() {
  if (!state.metricsMeta) state.metricsMeta = (await getJSON("/api/metrics/meta")).meta;
  return state.metricsMeta;
}

// visible metric keys for a view, honouring default_hidden and saved prefs
function visibleMetricKeys(storageKey) {
  const meta = state.metricsMeta || [];
  return colPrefs(storageKey, meta.map((m) => m.key),
                  meta.filter((m) => !m.default_hidden).map((m) => m.key));
}

// meta filtered to a view's visible metrics, for a panel to render
function visibleMeta(storageKey) {
  const vis = visibleMetricKeys(storageKey);
  return (state.metricsMeta || []).filter((m) => vis.has(m.key));
}

function renderMetricColPicker(target, storageKey, onChange) {
  const meta = state.metricsMeta || [];
  renderColPicker(target, storageKey, meta.map((m) => ({ key: m.key, label: m.label })),
                  visibleMetricKeys(storageKey), onChange,
                  meta.filter((m) => !m.default_hidden).map((m) => m.key));
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
  const meta = state.metricsMeta || [];  // expanded panel shows every metric
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
  // same per-game VOD/reflection panel as Overview's Recent games — a game in
  // a coaching period is exactly where you'd want the footage
  const rows = games.map((g) => {
    const gkey = `${g.match_id}:${g.my_puuid}`;
    const open = segmentUi.vodOpen.has(gkey);
    let html = `<tr>
      <td><button class="preset seg-toggle seg-vod-toggle" data-gkey="${escapeHtml(gkey)}"
        data-match="${escapeHtml(g.match_id)}" data-puuid="${escapeHtml(g.my_puuid)}"
        aria-expanded="${open}" title="Reflection and recording for this game"
        >${open ? "▾" : "▸"} 🎬</button></td>
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
    </tr>`;
    if (open) {
      const rec = recordingSection(g.match_id, g.my_puuid);
      html += `<tr class="games-row"><td colspan="11">
        <div class="vod-reflect">${reflectionSection(g.match_id, g.my_puuid, g)}</div>
        ${rec || `<p class="muted">No recording found for this game. Ascent recordings are
            matched by game id — press 🎬 in the header to re-scan.</p>`}
      </td></tr>`;
    }
    return html;
  }).join("");
  return `<table class="games-inner">
    <thead><tr><th></th><th>Date</th><th>Account</th><th>Me</th><th>Opponent</th><th>Opp. rank</th>
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
  renderProgress(segmentUi.segments);       // show "Loading…" straight away
  await Promise.all([
    ensureSegmentMetrics(segment),
    segment.session_id ? ensureSessionClips(segment.session_id) : Promise.resolve(),
    // OBS recordings + whether OBS is reachable at all, same lazy-on-expand rule
    segment.session_id
      ? ensureSessionRecordings(segment.session_id)
          .catch(() => srecUi.cache.set(segment.session_id, []))
      : Promise.resolve(),
    segment.session_id ? srecLoadStatus() : Promise.resolve(),
  ]);
  srecSyncPoll();
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
// metric-average columns (incl. lane deltas) available in the progress table,
// keyed "m:<metric>"; off by default. Cells read segment.metrics.
function progressMetricCols() {
  return (state.metricsMeta || []).map((m) => ({
    key: `m:${m.key}`, label: m.label,
    cell: (seg) => `<td>${fmtMetric(seg.metrics ? seg.metrics[m.key] : null, m)}</td>`,
  }));
}
function progressAllCols() { return [...PROGRESS_COLS, ...progressMetricCols()]; }
function progressVisibleKeys() {
  return colPrefs("cp-cols-progress", progressAllCols().map((c) => c.key),
                  PROGRESS_COLS.map((c) => c.key)); // base on, metric averages off
}
function progressVisibleCols() {
  const vis = progressVisibleKeys();
  return progressAllCols().filter((c) => vis.has(c.key));
}

// A period row IS its coaching session, so expanding one leads with that
// session's own record — notes, coach, clips — before the numbers that followed
// it. Baseline predates coaching and anchors to no session: it keeps its stats
// row (it's the reference everything else is read against) but has no record.
function sessionRecordPanel(segment) {
  if (!segment.session_id) {
    return `<p class="muted seg-session-empty">The 30 days before your first
      session — the reference the rest is measured against.</p>`;
  }
  const notes = segment.notes
    ? `<div class="md-body">${renderNotes(segment.notes)}</div>`
    : `<p class="muted">No notes for this session yet.</p>`;
  return `<div class="seg-session">
    <div class="learnings-head">
      <h4>Session notes</h4>
      <span class="seg-session-actions">
        <button class="preset icon-btn seg-session-edit" data-id="${segment.session_id}"
          title="Edit this session" aria-label="Edit this session">✎</button>
        <button class="preset icon-btn seg-session-delete" data-id="${segment.session_id}"
          title="Delete this session" aria-label="Delete this session">🗑</button>
      </span>
    </div>
    ${notes}
    ${sessionRecordingSection(segment.session_id)}
    ${clipsSection("session", segment.session_id, sessionUi.clips.get(segment.session_id))}
  </div>`;
}


function renderProgress(segments) {
  segmentUi.segments = segments;
  const target = $("#progress-table");
  if (!segments.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">
      No coaching sessions yet — add your first one with "+ Session" above.</div></div>`;
    return;
  }
  const visible = progressVisibleCols();
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
        <div class="period-text">
          <div class="period-title" title="${escapeHtml(periodTitle(segment))}"
            >${escapeHtml(periodTitle(segment))}</div>
          <div class="period-meta">
            <span class="muted period-sub">${fmtSegmentDates(segment)}</span>
            ${segment.coach ? `<span class="chip chip-plain session-coach"
              title="Coached by ${escapeHtml(segment.coach)}">🎓 ${escapeHtml(segment.coach)}</span>` : ""}
            ${segment.link ? `<a class="session-link" href="${escapeHtml(segment.link)}"
               target="_blank" rel="noopener noreferrer"
               title="Open the session recording">🔗</a>` : ""}
          </div></div>
      </div></td>` + visible.map((c) => (c.cell ? c.cell(segment) : cells[c.key])).join("") + `</tr>`;
    if (expanded) {
      html += `<tr class="games-row"><td colspan="${visible.length + 1}">
        ${sessionRecordPanel(segment)}${segmentMetricsPanel(segment)}</td></tr>`;
    }
    return html;
  });
  if (segmentUi.order === "desc") rows.reverse();   // newest session on top
  const body = rows.join("");
  const headers = { winrate: ' class="wr-col"' };
  const orderArrow = segmentUi.order === "desc" ? "↓" : "↑";
  const orderTitle = segmentUi.order === "desc"
    ? "Newest first — click for oldest first"
    : "Oldest first — click for newest first";
  target.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th class="period-th">Period
      <button type="button" id="progress-order" class="period-order"
        title="${orderTitle}" aria-label="${orderTitle}">${orderArrow}</button></th>` +
    visible.map((c) => `<th${headers[c.key] || ""}>${c.label}</th>`).join("") +
    `</tr></thead><tbody>${body}</tbody></table></div>`;
  target.querySelector("#progress-order").addEventListener("click", () => {
    segmentUi.order = segmentUi.order === "asc" ? "desc" : "asc";
    localStorage.setItem("cp-progress-order", segmentUi.order);
    renderProgress(segmentUi.segments);
  });
  target.querySelectorAll(".seg-session-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      const seg = segments.find((x) => x.session_id === +btn.dataset.id);
      if (seg) {
        openSessionModal({ id: seg.session_id, session_date: seg.session_date,
                           title: seg.session_title, coach: seg.coach, notes: seg.notes });
      }
    }));
  target.querySelectorAll(".seg-session-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this coaching session? Its notes, clips and recordings "
                   + "go with it — the video files themselves are kept.")) return;
      await fetch(`/api/sessions/${btn.dataset.id}`, { method: "DELETE" });
      loadProgress();
    }));
  wireClipsSection(target, async (ownerType, ownerId) => {
    sessionUi.clips.delete(+ownerId);
    await ensureSessionClips(+ownerId);
    renderProgress(segmentUi.segments);
  }, () => renderProgress(segmentUi.segments));
  // the OBS poll redraws on its own (elapsed clock, a recording stopped in
  // OBS), so it needs a way back into this render
  srecUi.rerender = () => renderProgress(segmentUi.segments);
  wireSessionRecordingSection(target, async (sessionId) => {
    await srecRefresh(sessionId);
    renderProgress(segmentUi.segments);
  }, () => renderProgress(segmentUi.segments));
  target.querySelectorAll(".seg-toggle").forEach((btn) =>
    btn.addEventListener("click", () => toggleSegment(segments[+btn.dataset.i])));
  target.querySelectorAll(".games-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const segment = segments.find((s) => segKey(s) === btn.dataset.key);
      if (segment) toggleSegmentGames(segment);
    }));
  wirePromoteButtons(target);
  target.querySelectorAll(".seg-vod-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const gkey = btn.dataset.gkey;
      if (segmentUi.vodOpen.has(gkey)) {
        segmentUi.vodOpen.delete(gkey);
        renderProgress(segments);
        return;
      }
      segmentUi.vodOpen.add(gkey);
      renderProgress(segments);           // show "Loading…" straight away
      await Promise.all([
        ensureReflection(btn.dataset.match, btn.dataset.puuid),
        ensureRecentRecording(btn.dataset.match, btn.dataset.puuid),
      ]);
      renderProgress(segments);
    }));
  wireReflectionSection(target, async (matchId, puuid) => {
    reflectionUi.cache.delete(reflectionKey(matchId, puuid));
    await ensureReflection(matchId, puuid);
    renderProgress(segments);
  }, () => renderProgress(segments));
  wireRecordingSection(target, async (matchId, puuid) => {
    recordingUi.cache.delete(recordingKey(matchId, puuid));
    await ensureRecentRecording(matchId, puuid);
    renderProgress(segments);
  });
}

const sessionUi = { clips: new Map(), coaches: [], modal: null };

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderNotes(notes) {
  if (!notes) return `<p class="muted">No notes yet — click edit to add some.</p>`;
  if (typeof marked !== "undefined") return marked.parse(notes);
  return `<pre>${escapeHtml(notes)}</pre>`; // fallback if vendor lib missing
}

// ---------- clips (shared by coaching sessions and block games) ----------

const clipsUi = { formOpen: new Set() }; // "ownerType:ownerId" keys with the add-form open

function clipsSection(ownerType, ownerId, clips) {
  const key = `${ownerType}:${ownerId}`;
  const items = clips === undefined
    ? `<p class="muted">Loading…</p>`
    : (clips.length ? clips.map((c) => `<div class="clip-item">
        <div class="clip-item-head">
          <span class="clip-label">${c.label ? escapeHtml(c.label) : `<span class="muted">clip</span>`}</span>
          <button class="preset icon-btn clip-delete" data-id="${c.id}"
            title="Delete clip" aria-label="Delete clip">🗑</button>
        </div>
        ${c.kind === "upload"
          ? `<video controls preload="metadata" src="${escapeHtml(c.play_url)}"></video>`
          : `<a href="${escapeHtml(c.play_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(c.play_url)}</a>`}
      </div>`).join("") : "");
  const form = clipsUi.formOpen.has(key)
    ? `<form class="clip-add-form">
        <input type="text" class="clip-label-input" placeholder='Label (optional) — e.g. "wave management @14min"'>
        <div class="clip-add-row">
          <input type="file" class="clip-file-input" accept=".mp4,.mov,.webm,.m4v,video/mp4,video/quicktime,video/webm">
          <span class="muted">or</span>
          <input type="url" class="clip-url-input" placeholder="paste a link (YouTube, Twitch…)">
        </div>
        <div class="session-actions">
          <button type="submit" class="preset">Add</button>
          <button type="button" class="preset clip-form-cancel">Cancel</button>
          <span class="muted clip-add-status"></span>
        </div>
      </form>`
    : `<button type="button" class="preset clip-form-open">+ Add clip</button>`;
  return `<div class="clips-section" data-owner-type="${ownerType}" data-owner-id="${ownerId}">
    <h5>Clips${clips && clips.length ? ` (${clips.length})` : ""}</h5>
    <div class="clips-list">${items}</div>
    ${form}
  </div>`;
}

// reload(ownerType, ownerId): async callback the caller supplies to refetch
// that owner's clips into its own cache and re-render its view.
// rerender(): cheap re-render of the caller's view without refetching —
// used for opening/closing the add form.
function wireClipsSection(container, reload, rerender) {
  const toggleForm = (btn, open) => {
    const section = btn.closest(".clips-section");
    const key = `${section.dataset.ownerType}:${section.dataset.ownerId}`;
    open ? clipsUi.formOpen.add(key) : clipsUi.formOpen.delete(key);
    rerender();
  };
  container.querySelectorAll(".clip-form-open").forEach((btn) =>
    btn.addEventListener("click", () => toggleForm(btn, true)));
  container.querySelectorAll(".clip-form-cancel").forEach((btn) =>
    btn.addEventListener("click", () => toggleForm(btn, false)));
  container.querySelectorAll(".clip-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this clip?")) return;
      await fetch(`/api/clips/${btn.dataset.id}`, { method: "DELETE" });
      const section = btn.closest(".clips-section");
      await reload(section.dataset.ownerType, section.dataset.ownerId);
    }));
  container.querySelectorAll(".clip-add-form").forEach((form) =>
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const section = form.closest(".clips-section");
      const status = form.querySelector(".clip-add-status");
      const label = form.querySelector(".clip-label-input").value;
      const file = form.querySelector(".clip-file-input").files[0];
      const url = form.querySelector(".clip-url-input").value.trim();
      if (!file && !url) { status.textContent = "add a file or a link"; return; }
      if (file && url) { status.textContent = "choose either a file or a link, not both"; return; }
      const fd = new FormData();
      fd.set("owner_type", section.dataset.ownerType);
      fd.set("owner_id", section.dataset.ownerId);
      fd.set("label", label);
      if (file) fd.set("file", file); else fd.set("url", url);
      status.textContent = "adding…";
      const response = await fetch("/api/clips", { method: "POST", body: fd });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      clipsUi.formOpen.delete(`${section.dataset.ownerType}:${section.dataset.ownerId}`);
      await reload(section.dataset.ownerType, section.dataset.ownerId);
    }));
}

/* ---------- OBS session recordings (coaching progress session cards) ----------

   Distinct from the Ascent VOD section below: that one attaches a recorded
   GAME to a match and chapters it from the match timeline. This one records a
   coaching SESSION with OBS — start/stop from the card, bookmark moments while
   it rolls, then review with those bookmarks as chapters.

   The file itself is never copied: the backend stores OBS's own output path
   and streams it. */

const SREC_POLL_MS = 2000;   // only while a recording is actually rolling

const srecUi = {
  cache: new Map(),      // sessionId -> [recording, ...] once fetched
  status: null,          // last /api/obs/status payload
  poll: null,            // interval id while recording
  rerender: null,        // set by renderProgress so the poll can redraw
  attachOpen: new Set(), // sessionIds with the "attach a file" form open
  markDraft: "",         // survives the re-renders the poll triggers
  busy: false,
};

// h:mm:ss past the hour — a coaching session runs long enough that fmtVideoTime's
// m:ss would read as "94:12".
function fmtSessionClock(ms) {
  const total = Math.max(0, Math.round((ms || 0) / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

async function ensureSessionRecordings(sessionId) {
  if (srecUi.cache.has(sessionId)) return;
  const body = await getJSON(`/api/sessions/${sessionId}/recordings`);
  srecUi.cache.set(sessionId, body.recordings);
}

async function srecRefresh(sessionId) {
  srecUi.cache.delete(sessionId);
  await ensureSessionRecordings(sessionId);
}

async function srecLoadStatus() {
  try {
    srecUi.status = await getJSON("/api/obs/status");
  } catch {
    srecUi.status = null;
  }
  return srecUi.status;
}

function srecActiveId() {
  return srecUi.status && srecUi.status.active ? srecUi.status.active.id : null;
}

// Poll only while something is recording: an idle session card should not open
// a connection to OBS every couple of seconds.
function srecSyncPoll() {
  const shouldPoll = srecActiveId() !== null;
  if (shouldPoll && !srecUi.poll) {
    srecUi.poll = setInterval(srecTick, SREC_POLL_MS);
  } else if (!shouldPoll && srecUi.poll) {
    clearInterval(srecUi.poll);
    srecUi.poll = null;
  }
}

async function srecTick() {
  const before = srecUi.status;
  const beforeId = srecActiveId();
  const status = await srecLoadStatus();
  if (!status) return;
  const activeId = srecActiveId();
  // cheap path: the clock ticks without redrawing the card (which would drop
  // focus from the bookmark input mid-typing)
  const elapsed = document.querySelector(".srec-elapsed");
  if (elapsed && status.active) elapsed.textContent = fmtSessionClock(status.duration_ms);
  if (activeId === beforeId && (!before || before.connected === status.connected)) return;
  if (beforeId !== null && activeId === null && before.active) {
    // it stopped (here or in OBS) — pull the finished row in
    await srecRefresh(before.active.session_id);
  }
  srecSyncPoll();
  if (srecUi.rerender) srecUi.rerender();
}

function srecMarkList(recording) {
  const marks = recording.marks || [];
  if (!marks.length) {
    return `<p class="muted srec-empty">No bookmarks yet.</p>`;
  }
  return `<ul class="srec-marks">${marks.map((m) => `<li>
    <button type="button" class="preset recording-seek srec-seek"
      data-recording="${recording.id}" data-ms="${m.offset_ms}"
      title="Jump to ${fmtSessionClock(m.offset_ms)}">⚑ ${fmtSessionClock(m.offset_ms)}</button>
    <span class="srec-mark-label">${m.label ? escapeHtml(m.label) : `<span class="muted">bookmark</span>`}</span>
    <button type="button" class="preset icon-btn srec-mark-delete" data-mark="${m.id}"
      title="Delete bookmark" aria-label="Delete bookmark">🗑</button>
  </li>`).join("")}</ul>`;
}

function srecCard(recording) {
  const live = recording.recording;
  const title = recording.label
    ? escapeHtml(recording.label)
    : `<span class="muted">${live ? "recording…" : "session recording"}</span>`;
  let body;
  if (live) {
    body = `<p class="muted">OBS is recording. Bookmark anything worth coming back to —
      each one becomes a chapter on the video once you stop.</p>`;
  } else if (!recording.video_path) {
    // stopped outside the app, so OBS never told us where the file went
    body = `<p class="muted">This recording ended outside the app, so its file is unknown.
      Attach it below, or forget this row.</p>`;
  } else if (!recording.file_exists) {
    body = `<p class="muted">The video file has moved or been deleted:<br>
      <code>${escapeHtml(recording.video_path)}</code></p>`;
  } else if (!recording.playable) {
    // OBS records .mkv by default and no browser plays Matroska
    body = `<p class="muted">Recorded as
      <code>${escapeHtml(recording.play_path.split(/[\\/]/).pop())}</code>, which browsers
      can't play. In OBS set <strong>Settings → Output → Recording Format</strong> to
      <strong>mp4</strong> (or run <strong>File → Remux Recordings</strong> on this one) and it
      will play here. Bookmarks are kept either way.</p>`;
  } else {
    body = `<video class="recording-video srec-video" controls preload="metadata"
      data-recording="${recording.id}" src="${escapeHtml(recording.play_url)}"></video>
      <div class="srec-actions">
        <button type="button" class="preset srec-mark-here" data-recording="${recording.id}">
          ⚑ Bookmark this moment</button>
        <input type="text" class="srec-mark-here-label" placeholder="what happens here (optional)">
      </div>`;
  }
  return `<div class="srec-card ${live ? "srec-live" : ""}" data-recording="${recording.id}">
    <div class="srec-head">
      <span class="srec-title">${title}</span>
      ${live ? "" : `<button type="button" class="preset icon-btn srec-forget"
        data-recording="${recording.id}" title="Forget this recording (the video file is kept)"
        aria-label="Forget this recording">🗑</button>`}
    </div>
    ${body}
    <div class="srec-marks-wrap">${srecMarkList(recording)}</div>
  </div>`;
}

function srecControls(sessionId) {
  const status = srecUi.status;
  const activeId = srecActiveId();
  const activeHere = status && status.active && status.active.session_id === sessionId;
  if (activeHere) {
    return `<div class="srec-controls">
      <span class="srec-rec-badge">● REC <span class="srec-elapsed">${
        fmtSessionClock(status.duration_ms)}</span></span>
      <input type="text" class="srec-mark-input" placeholder="bookmark this moment (optional note)"
        value="${escapeHtml(srecUi.markDraft)}">
      <button type="button" class="preset btn-primary srec-mark" data-session="${sessionId}">⚑ Bookmark</button>
      <button type="button" class="preset srec-stop" data-session="${sessionId}">■ Stop</button>
      <span class="muted srec-status"></span>
    </div>`;
  }
  if (activeId !== null) {
    return `<div class="srec-controls">
      <span class="muted">Another session is recording right now.</span>
    </div>`;
  }
  const unavailable = status && status.available === false
    ? `<p class="muted">OBS control needs the <code>websocket-client</code> package —
       reinstall <code>requirements.txt</code> to enable it.</p>`
    : "";
  const offline = status && status.available !== false && !status.connected
    ? `<p class="muted">OBS isn't reachable. Open OBS, turn on
       <strong>Tools → WebSocket Server Settings</strong>, then check the connection in
       Settings → OBS recording.</p>`
    : "";
  const attach = srecUi.attachOpen.has(sessionId)
    ? `<form class="srec-attach-form" data-session="${sessionId}">
        <input type="text" class="srec-attach-path" placeholder="full path to a video file"
          style="width:100%">
        <input type="text" class="srec-attach-label" placeholder="Label (optional)">
        <div class="session-actions">
          <button type="submit" class="preset">Attach</button>
          <button type="button" class="preset srec-attach-cancel">Cancel</button>
          <span class="muted srec-attach-status"></span>
        </div>
      </form>`
    : `<button type="button" class="preset srec-attach-open" data-session="${sessionId}">
        + Attach an existing file</button>`;
  return `<div class="srec-controls">
    <button type="button" class="preset btn-primary srec-start" data-session="${sessionId}">
      ● Record with OBS</button>
    ${attach}
    <span class="muted srec-status"></span>
  </div>${unavailable}${offline}`;
}

function sessionRecordingSection(sessionId) {
  const list = srecUi.cache.get(sessionId);
  const inner = list === undefined
    ? `<p class="muted">Loading…</p>`
    : `${list.map(srecCard).join("")}${srecControls(sessionId)}`;
  return `<div class="srec-section" data-session="${sessionId}">
    <h5>Recording${list && list.length ? ` (${list.length})` : ""}</h5>
    ${inner}
  </div>`;
}

// reload(sessionId) refetches one session's recordings and redraws; rerender()
// redraws without refetching (opening/closing the attach form).
function wireSessionRecordingSection(container, reload, rerender) {
  const sessionOf = (btn) => +btn.closest(".srec-section").dataset.session;
  const say = (btn, message) => {
    const status = btn.closest(".srec-section").querySelector(".srec-status");
    if (status) status.textContent = message;
  };
  // one place for "call the API, show what went wrong next to the button"
  const call = async (btn, url, options, pending) => {
    if (srecUi.busy) return null;
    srecUi.busy = true;
    say(btn, pending);
    try {
      const response = await fetch(url, options);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        say(btn, body.detail || `error ${response.status}`);
        return null;
      }
      return body;
    } finally {
      srecUi.busy = false;
    }
  };
  const postJSON = (payload) => ({
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

  container.querySelectorAll(".srec-start").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const sessionId = sessionOf(btn);
      const started = await call(btn, `/api/sessions/${sessionId}/recordings/start`,
                                 postJSON({}), "starting OBS…");
      if (!started) return;
      await srecLoadStatus();
      srecSyncPoll();
      await reload(sessionId);
    }));

  container.querySelectorAll(".srec-stop").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const sessionId = sessionOf(btn);
      const stopped = await call(btn, `/api/sessions/${sessionId}/recordings/stop`,
                                 postJSON({}), "stopping…");
      if (!stopped) return;
      await srecLoadStatus();
      srecSyncPoll();
      await reload(sessionId);
    }));

  // typed bookmark labels survive the poll's re-renders
  container.querySelectorAll(".srec-mark-input").forEach((input) =>
    input.addEventListener("input", () => { srecUi.markDraft = input.value; }));

  const addMark = async (btn, recordingId, payload) => {
    const added = await call(btn, `/api/session-recordings/${recordingId}/marks`,
                             postJSON(payload), "saving…");
    if (!added) return false;
    srecUi.markDraft = "";
    await reload(sessionOf(btn));
    return true;
  };

  container.querySelectorAll(".srec-mark").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const recordingId = srecActiveId();
      if (recordingId === null) return;
      const input = btn.closest(".srec-controls").querySelector(".srec-mark-input");
      await addMark(btn, recordingId, { label: input ? input.value : "" });
    }));
  // Enter in the bookmark box marks, so a session never needs the mouse
  container.querySelectorAll(".srec-mark-input").forEach((input) =>
    input.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const btn = input.closest(".srec-controls").querySelector(".srec-mark");
      if (btn) btn.click();
    }));

  // bookmarking during playback: the offset comes from the player, not OBS
  container.querySelectorAll(".srec-mark-here").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const card = btn.closest(".srec-card");
      const video = card.querySelector(".srec-video");
      const label = card.querySelector(".srec-mark-here-label");
      await addMark(btn, +btn.dataset.recording, {
        offset_ms: Math.round((video ? video.currentTime : 0) * 1000),
        label: label ? label.value : "",
      });
    }));

  container.querySelectorAll(".srec-seek").forEach((btn) =>
    btn.addEventListener("click", () => {
      const video = btn.closest(".srec-card").querySelector(".srec-video");
      if (!video) return;
      video.currentTime = +btn.dataset.ms / 1000;
      video.play();
    }));

  container.querySelectorAll(".srec-mark-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this bookmark?")) return;
      await fetch(`/api/session-marks/${btn.dataset.mark}`, { method: "DELETE" });
      await reload(sessionOf(btn));
    }));

  container.querySelectorAll(".srec-forget").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Forget this recording? The video file itself is kept.")) return;
      await fetch(`/api/session-recordings/${btn.dataset.recording}`, { method: "DELETE" });
      await reload(sessionOf(btn));
    }));

  container.querySelectorAll(".srec-attach-open").forEach((btn) =>
    btn.addEventListener("click", () => {
      srecUi.attachOpen.add(sessionOf(btn));
      rerender();
    }));
  container.querySelectorAll(".srec-attach-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      srecUi.attachOpen.delete(sessionOf(btn));
      rerender();
    }));
  container.querySelectorAll(".srec-attach-form").forEach((form) =>
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const sessionId = +form.dataset.session;
      const status = form.querySelector(".srec-attach-status");
      status.textContent = "attaching…";
      const response = await fetch(`/api/sessions/${sessionId}/recordings/attach`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: form.querySelector(".srec-attach-path").value,
          label: form.querySelector(".srec-attach-label").value,
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      srecUi.attachOpen.delete(sessionId);
      await reload(sessionId);
    }));
}

// ---------- game reflections (shared by Overview recent games and block game panels) ----------
// A quick per-game tag/note, independent of matchup notes / block learnings /
// sessions — a fast post-match reflection habit, not a full write-up.

// Suggestions, not analysis. Deliberately a SHORT list of things that are
// plainly true from the game's own numbers — "won lane, lost game" is a fact,
// "tilted" or "good macro" would be a guess dressed up as a read, and people
// take whatever the app offers as a verdict. Anything subtler is what the
// free-text tag box and the note are for.
const REFLECTION_TAG_RULES = [
  { tag: "Won lane, lost game", when: (g) => !g.win && g.lane_won },
  { tag: "Lost lane, won game", when: (g) => g.win && g.lane_lost },
  { tag: "High deaths", when: (g) => g.deaths >= 8 },
  { tag: "Deathless", when: (g) => g.deaths === 0 },
];
const MAX_REFLECTION_SUGGESTIONS = 3;

// game -> the few tags worth offering for it. `game` may be undefined where
// the caller has no stats to hand, in which case nothing is suggested rather
// than a generic list nobody asked for.
function reflectionSuggestions(game) {
  if (!game) return [];
  const facts = {
    win: Boolean(game.win),
    deaths: game.deaths ?? 0,
    // Riot's binary laning flag at 14m — the same signal the lane column uses
    lane_won: game.lane_adv_late != null && game.lane_adv_late >= 1,
    lane_lost: game.lane_adv_late != null && game.lane_adv_late < 1,
  };
  return REFLECTION_TAG_RULES.filter((r) => r.when(facts))
    .map((r) => r.tag).slice(0, MAX_REFLECTION_SUGGESTIONS);
}

const reflectionUi = {
  cache: new Map(),        // "matchId:puuid" -> {tags, note} once fetched
  editingNote: new Set(),  // "matchId:puuid" keys with the note editor open
};

/* ---------- recordings (local Ascent VODs) ----------

   Shared by the Overview's Recent games and a block game's stats panel, the
   same way clipsSection/reflectionSection are. The video is a local file
   streamed by the backend, so it plays inline and can be seeked — which is the
   point: each death from the match timeline becomes a clickable marker that
   jumps the video to that moment. */

// How far BEFORE a marker to drop the playhead. Every marker is the moment a
// thing already happened, so landing on it shows the aftermath and hides the
// cause; five seconds covers the approach without overshooting the previous
// event in a busy fight.
const SEEK_LEAD_MS = 5000;

const recordingUi = {
  listOpen: new Set(),     // recording uuids whose itemised event list is open
  cache: new Map(),          // "matchId:puuid" -> [recording, ...] once fetched
  recordedMatches: null,     // Set of match ids that have a recording, or null
  upload: { uuid: null, progress: 0, error: null, timer: null },
  descriptions: new Map(),        // uuid -> generated YouTube description
  descriptionOpen: new Set(),     // uuids with the description block expanded
  markTab: new Map(),             // uuid -> which event tab is selected
};

function recordingKey(matchId, puuid) { return `${matchId}:${puuid}`; }

/* Re-read Ascent: the recordings database (video files -> games) and the logs
   (League's event feed -> chapter timestamps). Reports what it found rather
   than just "done", because "0 imported" is usually explained by the counts. */
function recordingSyncSummary(body) {
  const bits = [`${body.imported} linked of ${body.seen} recordings`];
  if (body.skipped_unmatched) bits.push(`${body.skipped_unmatched} not crawled yet`);
  if (body.skipped_missing_file) bits.push(`${body.skipped_missing_file} file missing`);
  const events = body.events || {};
  if (events.events) bits.push(`${events.events} events from ${events.imported} games`);
  else if (events.error) bits.push(`events: ${events.error}`);
  return `${bits.join(" · ")} ✓`;
}

// dropped so expanded panels refetch instead of showing what was cached
function clearRecordingCaches() {
  recordingUi.recordedMatches = null;
  recordingUi.cache.clear();
  recordingUi.descriptions.clear();
}

async function testObsConnection() {
  const status = $("#obs-test-status");
  status.classList.remove("status-error");
  status.textContent = "Connecting…";
  // send what is typed rather than what is saved, so the values can be checked
  // before committing them
  const response = await fetch("/api/obs/test", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      host: $("#setting-obs-host").value.trim(),
      port: parseInt($("#setting-obs-port").value, 10) || 4455,
      password: $("#setting-obs-password").value,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    status.classList.add("status-error");
    status.textContent = body.detail || `error ${response.status}`;
    return;
  }
  const where = body.record_directory ? ` — recording to ${body.record_directory}` : "";
  status.textContent = `Connected to OBS ${body.obs_version}${
    body.recording ? " (recording now)" : ""}${where}`;
}

async function syncRecordings() {
  const status = $("#sync-recordings-status");
  status.classList.remove("status-error");
  status.textContent = "Syncing…";
  try {
    const response = await fetch("/api/recordings/sync", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `error ${response.status}`);
    status.textContent = recordingSyncSummary(body);
    clearRecordingCaches();
  } catch (err) {
    status.classList.add("status-error");
    status.textContent = String(err.message || err);
  }
}

// The Ascent scan summary is several clauses long, so it lives in a popover
// hung off the 🎬 button instead of as inline header text (which wrapped the
// header onto a second line). The badge is the affordance: ⏳ while scanning,
// ✓/! when there's a result to read.
function setRecordingStatus(text, { busy = false, error = false } = {}) {
  const badge = $("#recordings-badge");
  const pop = $("#recordings-pop");
  pop.textContent = text || "";
  pop.classList.toggle("status-error", error);
  badge.classList.toggle("hidden", !text);
  badge.classList.toggle("status-error", error);
  badge.textContent = busy ? "⏳" : (error ? "!" : "✓");
  badge.title = text ? `${text} — click for details` : "";
  if (busy) {  // don't pop open on its own; the badge invites the click
    pop.classList.add("hidden");
    badge.setAttribute("aria-expanded", "false");
  }
}

function toggleRecordingPop(force) {
  const pop = $("#recordings-pop");
  const open = force === undefined ? pop.classList.contains("hidden") : force;
  pop.classList.toggle("hidden", !open);
  $("#recordings-badge").setAttribute("aria-expanded", String(open));
}

// the header 🎬 button — same scan, reachable without going into Settings
async function rescanRecordings() {
  const btn = $("#recordings-btn");
  // its own status span: #crawl-status is owned by the crawl poller, which
  // would wipe this the moment it next ticked
  if (btn.disabled) return;
  btn.disabled = true;
  setRecordingStatus("Scanning Ascent…", { busy: true });
  try {
    const response = await fetch("/api/recordings/sync", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `error ${response.status}`);
    clearRecordingCaches();
    // re-render whichever view is showing recordings so they appear at once
    if (state.mainView === "blocks") initBlocks();
    else if (state.mainView === "overview") await refresh();
    setRecordingStatus(recordingSyncSummary(body));   // after the re-render
  } catch (err) {
    setRecordingStatus(`Ascent scan failed — ${err.message || err}`, { error: true });
  } finally {
    btn.disabled = false;
  }
}

// one cheap request so games tables can show a 🎬 marker without a fetch per row
async function loadRecordedMatches(force = false) {
  if (recordingUi.recordedMatches && !force) return recordingUi.recordedMatches;
  try {
    const data = await getJSON("/api/recordings/matches");
    recordingUi.recordedMatches = new Set(data.match_ids || []);
  } catch {
    recordingUi.recordedMatches = new Set(); // feature simply stays hidden
  }
  return recordingUi.recordedMatches;
}

function hasRecording(matchId) {
  return Boolean(recordingUi.recordedMatches && recordingUi.recordedMatches.has(matchId));
}

function fmtVideoTime(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function recordingSection(matchId, puuid) {
  const key = recordingKey(matchId, puuid);
  const list = recordingUi.cache.get(key);
  if (list === undefined) {
    return `<div class="recording-section" data-match="${escapeHtml(matchId)}"
      data-puuid="${escapeHtml(puuid)}"><h5>Recording</h5><p class="muted">Loading…</p></div>`;
  }
  if (!list.length) return "";
  const body = list.map((r) => recordingCard(r)).join("");
  return `<div class="recording-section" data-match="${escapeHtml(matchId)}"
    data-puuid="${escapeHtml(puuid)}"><h5>Recording</h5>${body}</div>`;
}

function recordingCard(r) {
  if (!r.file_exists) {
    return `<div class="recording-card">
      <p class="muted">The video file has moved or been deleted:<br>
        <code>${escapeHtml(r.video_path)}</code></p>
      <button type="button" class="preset recording-forget" data-uuid="${escapeHtml(r.uuid)}">
        Forget this recording</button>
    </div>`;
  }
  const marks = recordingSeekRow(r);
  const uploading = recordingUi.upload.uuid === r.uuid;
  // Only offer "Upload to YouTube" when it can actually happen — i.e. the
  // optional Google libraries are installed AND an OAuth client is configured
  // (youtube_ready covers both). Otherwise the card shows the plain
  // reveal-the-file action plus an ℹ explaining how to turn upload on, rather
  // than a YouTube button that would dead-end.
  const reveal = `<button type="button" class="preset recording-manual"
    data-uuid="${escapeHtml(r.uuid)}"
    title="Open the folder containing this recording">📁 Show file</button>`;
  const uploadBtn = state.youtubeReady
    ? `<button type="button" class="preset btn-primary recording-upload"
         data-uuid="${escapeHtml(r.uuid)}" ${uploading ? "disabled" : ""}
         title="One-click upload using your configured Google OAuth client"
         >${uploading ? "Uploading…" : "⬆ Upload to YouTube"}</button>`
    : `<button type="button" class="preset icon-btn recording-yt-info"
         aria-label="Why is there no YouTube upload button?"
         title="YouTube upload isn't set up. Install the optional Google libraries (requirements-youtube.txt) and add your OAuth client secrets in Settings → Recordings &amp; YouTube. Click to open Settings.">ℹ</button>`;
  const yt = r.youtube_video_id
    ? `<a class="preset recording-yt-link" href="${escapeHtml(r.youtube_url)}"
         target="_blank" rel="noopener noreferrer">▶ On YouTube</a>`
    : `${uploadBtn}${reveal}`;
  const progress = uploading
    ? `<div class="recording-progress"><div class="recording-progress-fill"
         style="width:${Math.round(recordingUi.upload.progress * 100)}%"></div></div>`
    : "";
  const error = recordingUi.upload.error && recordingUi.upload.uuid === r.uuid
    ? `<span class="muted status-error">${escapeHtml(recordingUi.upload.error)}</span>` : "";
  return `<div class="recording-card" data-uuid="${escapeHtml(r.uuid)}">
    <div class="recording-main">
      <video class="recording-video" controls preload="metadata"
        src="/api/recordings/${encodeURIComponent(r.uuid)}/file"></video>
      ${recordingMap(r)}
    </div>
    ${marks}
    <div class="recording-actions">
      ${yt}
      <label class="recording-offset-label" title="Nudge if the video and game clocks drift">
        offset
        <input type="number" class="recording-offset" data-uuid="${escapeHtml(r.uuid)}"
          step="1000" value="${r.offset_ms}"> ms
      </label>
      ${error}
    </div>
    <span class="muted recording-manual-status"></span>
    ${recordingDescriptionBlock(r)}
    ${progress}
  </div>`;
}

/* ---------- the mini-map beside the player ----------

   Every stored event has a map position, so the same schematic Rift the Trends
   death map draws doubles as a clickable index of the game: each dot seeks the
   video to that moment. Reuses heatmapPoint and the MAP_ and HEATMAP_
   constants from trends.js — that file loads after app.js, but this only runs
   on click, long after everything is parsed. */

/* A schematic Summoner's Rift, drawn from scratch as plain SVG — three lanes,
   the four jungle quadrants, the river running corner to corner, both bases,
   the turret line and the Baron/Dragon pits. Deliberately an abstraction of
   the real map's geometry rather than a copy of any map artwork.

   Everything is expressed in Riot's own map coordinates (0..MAP_X_MAX by
   0..MAP_Y_MAX, origin bottom-left) and projected through heatmapPoint, so the
   drawing and the plotted events share one coordinate space. */
const RIFT = {
  blueBase: [1900, 1900],
  redBase: [12900, 13000],
  // each lane runs base -> base; the bends are where the lane turns the corner
  topLane: [[2300, 3400], [1250, 7000], [1250, 11600], [2500, 13350],
            [7000, 13650], [11700, 13400]],
  botLane: [[3400, 2300], [7000, 1250], [11600, 1250], [13350, 2500],
            [13650, 7000], [13400, 11700]],
  midLane: [[3100, 3100], [6200, 6100], [8700, 8800], [11700, 11700]],
  river: [[2400, 12600], [6600, 8400], [8400, 6600], [12600, 2400]],
  baron: [4950, 10400],
  dragon: [9850, 4400],
  // fraction along each lane where the outer / inner / inhibitor turrets sit,
  // measured from the blue end and mirrored for red
  turretsAt: [0.17, 0.36, 0.5],
};

// a point a fraction `t` along a polyline, in map coordinates
function riftPointAlong(points, t) {
  const segments = [];
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    const d = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    segments.push(d);
    total += d;
  }
  let travelled = t * total;
  for (let i = 0; i < segments.length; i++) {
    if (travelled <= segments[i]) {
      const f = segments[i] ? travelled / segments[i] : 0;
      return [points[i][0] + (points[i + 1][0] - points[i][0]) * f,
              points[i][1] + (points[i + 1][1] - points[i][1]) * f];
    }
    travelled -= segments[i];
  }
  return points[points.length - 1];
}

/* The real minimap, from the same Data Dragon CDN the app already hotlinks
   champion/rune/item icons from. The hand-drawn schematic is rendered first and
   left underneath, so an offline or failed image degrades to something usable
   rather than an empty square.

   DDragon's map11.png covers the same coordinate space events are recorded in,
   so it drops straight into the padded square heatmapPoint projects into. */
function riftBackdrop() {
  const S = HEATMAP_SIZE;
  const inner = S - HEATMAP_PAD * 2;
  const image = state.ddragonVersion
    ? `<image class="rift-image"
         href="https://ddragon.leagueoflegends.com/cdn/${state.ddragonVersion}/img/map/map11.png"
         x="${HEATMAP_PAD}" y="${HEATMAP_PAD}" width="${inner}" height="${inner}"
         preserveAspectRatio="none"/>` : "";
  return riftSchematic() + image;
}

function riftSchematic() {
  const S = HEATMAP_SIZE;
  const path = (pts) => pts.map((p) => heatmapPoint(p[0], p[1]).map((v) => v.toFixed(1)).join(","))
    .join(" ");
  const at = (p) => heatmapPoint(p[0], p[1]).map((v) => v.toFixed(1));
  const lanes = [RIFT.topLane, RIFT.midLane, RIFT.botLane];

  // turrets: three per lane per side, mirrored around the lane's midpoint
  const turrets = lanes.flatMap((lane) =>
    RIFT.turretsAt.flatMap((t) => [[t, "rift-turret-blue"], [1 - t, "rift-turret-red"]]
      .map(([frac, cls]) => {
        const [x, y] = at(riftPointAlong(lane, frac));
        return `<rect class="rift-turret ${cls}" x="${x - 2}" y="${y - 2}" width="4" height="4"/>`;
      }))).join("");

  const [baronX, baronY] = at(RIFT.baron);
  const [dragonX, dragonY] = at(RIFT.dragon);
  const [blueX, blueY] = at(RIFT.blueBase);
  const [redX, redY] = at(RIFT.redBase);

  return `
    <rect class="rift-field" x="${HEATMAP_PAD}" y="${HEATMAP_PAD}"
      width="${S - HEATMAP_PAD * 2}" height="${S - HEATMAP_PAD * 2}" rx="6"/>
    <polyline class="rift-river" points="${path(RIFT.river)}"/>
    ${lanes.map((l) => `<polyline class="rift-lane" points="${path(l)}"/>`).join("")}
    ${turrets}
    <circle class="rift-pit" cx="${baronX}" cy="${baronY}" r="6"/>
    <text class="rift-pit-label" x="${baronX}" y="${+baronY + 3}">B</text>
    <circle class="rift-pit" cx="${dragonX}" cy="${dragonY}" r="6"/>
    <text class="rift-pit-label" x="${dragonX}" y="${+dragonY + 3}">D</text>
    <circle class="rift-base rift-base-blue" cx="${blueX}" cy="${blueY}" r="9"/>
    <circle class="rift-base rift-base-red" cx="${redX}" cy="${redY}" r="9"/>`;
}

// event type -> [css class, radius]. Deaths and kills are the ones you scan
// for, so they're largest; assists sit behind them.
const RECORDING_MAP_MARKS = {
  death: ["rec-map-death", 5.5],
  kill: ["rec-map-kill", 5.5],
  assist: ["rec-map-assist", 3.5],
  tower: ["rec-map-tower", 5],
  inhibitor: ["rec-map-tower", 6],
  objective: ["rec-map-objective", 6],
};
// kills first, then deaths — reading your own outcomes before the map's
const RECORDING_MAP_LEGEND = [
  ["kill", "Kills"], ["death", "Deaths"], ["assist", "Assists"],
  ["tower", "Towers"], ["objective", "Objectives"],
];

/* One clickable chip per event, in game order — the same list the chapters
   are built from, so anything with a timestamp there is reachable here. Glyph
   and colour match the map's markers so the two read as one thing. */
const RECORDING_MARK_GLYPHS = {
  kill: "⚔", death: "☠", assist: "✚",
  tower: "🏰", inhibitor: "🛡", objective: "🐉",
};

// tab -> which event types it covers. "all" is everything; structures group
// towers with inhibitors because that's how you think about them.
const RECORDING_MARK_TABS = [
  ["all", "All", null],
  ["kill", "⚔ Kills", ["kill"]],
  ["death", "☠ Deaths", ["death"]],
  ["assist", "✚ Assists", ["assist"]],
  ["tower", "🏰 Structures", ["tower", "inhibitor"]],
  ["objective", "🐉 Objectives", ["objective"]],
];

function markLabel(m, ordinals) {
  const detail = (m.detail || "").trim();
  if (detail) {
    return detail.startsWith("-")
      ? `Lost ${detail.slice(1).toLowerCase()}` : detail;
  }
  // kills/deaths/assists have no detail — number them as they happened
  ordinals[m.event_type] = (ordinals[m.event_type] || 0) + 1;
  const name = { kill: "Kill", death: "Death", assist: "Assist" }[m.event_type]
    || m.event_type;
  return `${name} ${ordinals[m.event_type]}`;
}

function recordingSeekRow(r) {
  const marks = r.marks || [];
  if (!marks.length) return "";
  // label every mark once, in game order, so numbering is stable per tab
  const ordinals = {};
  const labelled = marks.map((m) => ({ ...m, label: markLabel(m, ordinals) }));

  const active = recordingUi.markTab.get(r.uuid) || "all";
  const tabs = RECORDING_MARK_TABS.map(([key, label, kinds]) => {
    const count = kinds ? labelled.filter((m) => kinds.includes(m.event_type)).length
      : labelled.length;
    if (!count) return "";
    return `<button type="button" class="recording-mark-tab ${key === active ? "active" : ""}"
      data-uuid="${escapeHtml(r.uuid)}" data-tab="${key}">${label} ${count}</button>`;
  }).join("");

  const kinds = (RECORDING_MARK_TABS.find((t) => t[0] === active) || [])[2];
  const shown = kinds ? labelled.filter((m) => kinds.includes(m.event_type)) : labelled;
  const rows = shown.map((m) => {
    const against = (m.detail || "").startsWith("-");
    const cls = (RECORDING_MAP_MARKS[m.event_type] || ["", 0])[0];
    return `<li><button type="button" class="recording-seek ${cls}
      ${against ? "recording-seek-against" : ""}"
      data-uuid="${escapeHtml(r.uuid)}" data-ms="${m.video_ms}"
      title="Jump to ${fmtVideoTime(m.video_ms)}">
      <span class="recording-seek-glyph">${RECORDING_MARK_GLYPHS[m.event_type] || "•"}</span>
      <span class="recording-seek-time">${fmtVideoTime(m.video_ms)}</span>
      <span class="recording-seek-label">${escapeHtml(m.label)}</span>
    </button></li>`;
  }).join("");

  // A timeline is the primary way in: every event placed where it happens in
  // the video, click to jump. The itemised list is the same data read the slow
  // way, so it's collapsed until asked for.
  const span = (r.duration_s || 0) * 1000
    || Math.max(...labelled.map((m) => m.video_ms), 1);
  const ticks = labelled.map((m) => {
    const pct = Math.min(100, Math.max(0, (m.video_ms / span) * 100));
    const against = (m.detail || "").startsWith("-");
    const cls = (RECORDING_MAP_MARKS[m.event_type] || ["", 0])[0];
    return `<button type="button" class="recording-seek rt-mark ${cls}
      ${against ? "recording-seek-against" : ""}" style="left:${pct.toFixed(2)}%"
      data-uuid="${escapeHtml(r.uuid)}" data-ms="${m.video_ms}"
      title="${escapeHtml(m.label)} @ ${fmtVideoTime(m.video_ms)} — click to jump"
      aria-label="${escapeHtml(m.label)} at ${fmtVideoTime(m.video_ms)}"
      >${RECORDING_MARK_GLYPHS[m.event_type] || "•"}</button>`;
  }).join("");
  const listOpen = recordingUi.listOpen.has(r.uuid);
  return `<div class="recording-marks">
    <div class="recording-timeline" aria-label="Game events on the recording timeline">
      <div class="rt-track"></div>
      ${ticks}
    </div>
    <div class="rt-scale muted"><span>0:00</span><span>${fmtVideoTime(span)}</span></div>
    <button type="button" class="preset recording-list-toggle" data-uuid="${escapeHtml(r.uuid)}"
      aria-expanded="${listOpen}">${listOpen ? "▾" : "▸"} Event list (${labelled.length})</button>
    ${listOpen ? `<div class="view-toggle recording-mark-tabs" role="tablist">${tabs}</div>
    <ul class="recording-mark-list">${rows}</ul>` : ""}
  </div>`;
}

function recordingMap(r) {
  const events = (r.events || []).filter(
    (e) => e.x != null && e.y != null && RECORDING_MAP_MARKS[e.event_type]);
  if (!events.length) {
    // Silently rendering nothing here is what makes the map look broken: the
    // usual cause is that this game's events came from Ascent's log, which is
    // built on League's Live Client Data feed and carries no coordinates.
    // Only the match timeline has positions, so say what to run.
    const fromLog = (r.events || []).length > 0;
    return `<div class="recording-map recording-map-empty"><p class="muted">${fromLog
      ? `No positions recorded for this game — its events came from Ascent's log, which has
         timings but no coordinates. Run <code>./crawl.sh --recompute-map-events</code> to
         re-derive them from the match timeline.`
      : `No map events for this game yet. Run <code>./crawl.sh --backfill-map-events</code>
         to pull them from the match timeline.`}</p></div>`;
  }
  const S = HEATMAP_SIZE;
  const dots = events.map((e) => {
    const [x, y] = heatmapPoint(e.x, e.y);
    const [cls, radius] = RECORDING_MAP_MARKS[e.event_type];
    // "-" on a detail marks an event that went against us — draw it hollow
    const against = (e.detail || "").startsWith("-");
    const label = `${e.detail ? (against ? `Lost ${e.detail.slice(1).toLowerCase()}` : e.detail)
      : e.event_type} @ ${fmtVideoTime(e.video_ms)} — click to play from ${
      SEEK_LEAD_MS / 1000}s before`;
    return `<circle class="rec-map-dot ${cls} ${against ? "rec-map-against" : ""}"
      cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${radius}"
      data-ms="${e.video_ms}" tabindex="0" role="button"
      ><title>${escapeHtml(label)}</title></circle>`;
  }).join("");
  const counts = events.reduce((acc, e) => {
    acc[e.event_type] = (acc[e.event_type] || 0) + 1;
    return acc;
  }, {});
  const legend = RECORDING_MAP_LEGEND.filter(([key]) => counts[key])
    .map(([key, label]) => `<span class="rec-map-key">
      <span class="rec-map-swatch ${RECORDING_MAP_MARKS[key][0]}"></span>${label} ${counts[key]}
    </span>`).join("");
  return `<div class="recording-map">
    <svg class="rec-map-svg" viewBox="0 0 ${S} ${S}" role="img"
      aria-label="Where kills, deaths, towers and objectives happened">
      ${riftBackdrop()}
      ${dots}
    </svg>
    <div class="muted rec-map-legend">${legend}</div>
  </div>`;
}

/* The generated YouTube description — matchup summary plus a chapter per
   death, so the uploaded video is navigable. Collapsed by default: it's only
   needed at upload time. */
function recordingDescriptionBlock(r) {
  const open = recordingUi.descriptionOpen.has(r.uuid);
  const text = recordingUi.descriptions.get(r.uuid);
  if (!open) {
    return `<button type="button" class="preset seg-toggle recording-desc-toggle"
      data-uuid="${escapeHtml(r.uuid)}" aria-expanded="false">▸ Description &amp; chapters</button>`;
  }
  return `<div class="recording-desc">
    <button type="button" class="preset seg-toggle recording-desc-toggle"
      data-uuid="${escapeHtml(r.uuid)}" aria-expanded="true">▾ Description &amp; chapters</button>
    ${text === undefined
      ? `<p class="muted">Loading…</p>`
      : `<textarea class="recording-desc-text" rows="10" readonly>${escapeHtml(text)}</textarea>
         <div class="session-actions">
           <button type="button" class="preset recording-desc-copy"
             data-uuid="${escapeHtml(r.uuid)}">📋 Copy description</button>
           <span class="muted recording-desc-status"></span>
         </div>
         <p class="muted">Paste into YouTube's description box — timestamps become
           clickable chapters.</p>`}
  </div>`;
}

async function ensureRecordingDescription(uuid, matchId, puuid) {
  if (recordingUi.descriptions.has(uuid)) return;
  try {
    const data = await getJSON(
      `/api/recordings/${encodeURIComponent(uuid)}/description?puuid=${encodeURIComponent(puuid)}`);
    recordingUi.descriptions.set(uuid, data.description || "");
  } catch {
    recordingUi.descriptions.set(uuid, "");
  }
}

/* reload(matchId, puuid): refetch that game's recordings into the caller's
   cache and re-render, mirroring wireReflectionSection's contract. */
function wireRecordingSection(container, reload) {
  container.querySelectorAll(".recording-desc-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const uuid = btn.dataset.uuid;
      const section = btn.closest(".recording-section");
      if (recordingUi.descriptionOpen.has(uuid)) {
        recordingUi.descriptionOpen.delete(uuid);
      } else {
        recordingUi.descriptionOpen.add(uuid);
        if (section) {
          await ensureRecordingDescription(uuid, section.dataset.match, section.dataset.puuid);
        }
      }
      if (section) await reload(section.dataset.match, section.dataset.puuid);
    }));

  container.querySelectorAll(".recording-desc-copy").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const status = btn.parentElement.querySelector(".recording-desc-status");
      try {
        await navigator.clipboard.writeText(recordingUi.descriptions.get(btn.dataset.uuid) || "");
        status.textContent = "copied ✓";
      } catch {
        // clipboard blocked — select it so Ctrl+C still works
        const area = btn.closest(".recording-desc").querySelector(".recording-desc-text");
        if (area) area.select();
        status.textContent = "selected — press Ctrl+C";
      }
    }));

  // both the ☠ buttons and the map dots do the same thing: jump the video
  const seekTo = (el) => {
    const card = el.closest(".recording-card");
    const video = card && card.querySelector("video");
    if (!video) return;
    // land SEEK_LEAD_MS before the event: a death or objective only makes
    // sense with the run-up to it, and dropping the user exactly on the
    // timestamp meant scrubbing backwards every single time
    video.currentTime = Math.max(0, (+el.dataset.ms) - SEEK_LEAD_MS) / 1000;
    video.play().catch(() => { /* autoplay blocked — the seek still landed */ });
  };
  container.querySelectorAll(".recording-seek").forEach((btn) =>
    btn.addEventListener("click", () => seekTo(btn)));
  container.querySelectorAll(".recording-list-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const uuid = btn.dataset.uuid;
      if (recordingUi.listOpen.has(uuid)) recordingUi.listOpen.delete(uuid);
      else recordingUi.listOpen.add(uuid);
      const card = btn.closest(".recording-card");
      const section = btn.closest(".recording-section");
      const list = recordingUi.cache.get(
        recordingKey(section.dataset.match, section.dataset.puuid)) || [];
      const recording = list.find((x) => x.uuid === uuid);
      if (!card || !recording) return;
      card.querySelector(".recording-marks").outerHTML = recordingSeekRow(recording);
      wireRecordingSection(card, reload);
    }));
  container.querySelectorAll(".recording-mark-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      recordingUi.markTab.set(tab.dataset.uuid, tab.dataset.tab);
      // swap just this card's marks block — a full reload would refetch and
      // scroll the page for what is a local view change
      const card = tab.closest(".recording-card");
      const section = tab.closest(".recording-section");
      const list = recordingUi.cache.get(
        recordingKey(section.dataset.match, section.dataset.puuid)) || [];
      const recording = list.find((x) => x.uuid === tab.dataset.uuid);
      if (!card || !recording) return;
      card.querySelector(".recording-marks").outerHTML = recordingSeekRow(recording);
      wireRecordingSection(card, reload);
    }));

  container.querySelectorAll(".rec-map-dot").forEach((dot) => {
    dot.addEventListener("click", () => seekTo(dot));
    dot.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); seekTo(dot); }
    });
  });

  container.querySelectorAll(".recording-offset").forEach((input) =>
    input.addEventListener("change", async () => {
      await fetch(`/api/recordings/${encodeURIComponent(input.dataset.uuid)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offset_ms: Math.round(+input.value) || 0 }),
      });
      const section = input.closest(".recording-section");
      if (section) await reload(section.dataset.match, section.dataset.puuid);
    }));

  container.querySelectorAll(".recording-forget").forEach((btn) =>
    btn.addEventListener("click", async () => {
      // the file itself is never touched — say so, since "forget" reads scary
      if (!confirm("Forget this recording? The video file on disk is not deleted.")) return;
      await fetch(`/api/recordings/${encodeURIComponent(btn.dataset.uuid)}`, { method: "DELETE" });
      recordingUi.recordedMatches = null;
      const section = btn.closest(".recording-section");
      if (section) await reload(section.dataset.match, section.dataset.puuid);
    }));

  /* Manual upload: no OAuth, no quota, no private-lock. Does the three things
     that turn "upload this" into one drag — copies the path, opens Explorer
     with the file selected, and opens YouTube's upload page. Pasting the path
     into YouTube's file picker is usually quicker than dragging between
     windows, so the path is offered either way. */
  // the ℹ shown in place of the upload button when YouTube isn't set up
  container.querySelectorAll(".recording-yt-info").forEach((btn) =>
    btn.addEventListener("click", () => setMainView("settings")));
  container.querySelectorAll(".recording-manual").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const status = btn.closest(".recording-card").querySelector(".recording-manual-status");
      const response = await fetch(
        `/api/recordings/${encodeURIComponent(btn.dataset.uuid)}/reveal`, { method: "POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (status) {
          status.classList.add("status-error");
          status.textContent = body.detail || `error ${response.status}`;
        }
        return;
      }
      let copied = false;
      try {
        await navigator.clipboard.writeText(body.video_path);
        copied = true;
      } catch { /* clipboard blocked — Explorer is open anyway */ }
      window.open("https://www.youtube.com/upload", "_blank", "noopener");
      if (status) {
        status.classList.remove("status-error");
        status.textContent = copied
          ? "Path copied — paste it into YouTube's file picker, or drag from Explorer"
          : "Explorer opened — drag the file into the YouTube tab";
      }
    }));

  container.querySelectorAll(".recording-upload").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const section = btn.closest(".recording-section");
      const card = btn.closest(".recording-card");
      const privacy = state.youtubePrivacy || "private";
      // uploading publishes to the user's channel — always confirm first
      if (!confirm(`Upload this recording to YouTube as ${privacy}?`)) return;
      recordingUi.upload = { uuid: btn.dataset.uuid, progress: 0, error: null, timer: null };
      btn.disabled = true;
      btn.textContent = "Uploading…";
      const response = await fetch(
        `/api/recordings/${encodeURIComponent(btn.dataset.uuid)}/youtube`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
          title: recordingTitle(card), privacy,
          // lets the server generate the chapter list server-side
          puuid: section ? section.dataset.puuid : undefined,
        }),
        });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        recordingUi.upload.error = body.detail || `error ${response.status}`;
        recordingUi.upload.uuid = null;
        if (section) await reload(section.dataset.match, section.dataset.puuid);
        return;
      }
      pollUploadStatus(section, reload);
    }));
}

// a readable default title from whatever the surrounding row already shows
function recordingTitle(card) {
  const row = card && card.closest("tr, .block-card, .session-card");
  const champs = row ? row.querySelectorAll(".champ-cell img, .champ-cell") : [];
  const text = champs.length ? [...champs].map((c) => c.getAttribute("alt") || "").filter(Boolean).join(" vs ") : "";
  return text || "Coach Potato VOD";
}

function pollUploadStatus(section, reload) {
  clearInterval(recordingUi.upload.timer);
  recordingUi.upload.timer = setInterval(async () => {
    let status;
    try {
      status = await getJSON("/api/recordings/upload-status");
    } catch {
      return;
    }
    recordingUi.upload.progress = status.progress || 0;
    if (!status.running) {
      clearInterval(recordingUi.upload.timer);
      recordingUi.upload.error = status.error;
      recordingUi.upload.uuid = status.error ? recordingUi.upload.uuid : null;
      if (section) await reload(section.dataset.match, section.dataset.puuid);
      return;
    }
    const fill = document.querySelector(".recording-progress-fill");
    if (fill) fill.style.width = `${Math.round(recordingUi.upload.progress * 100)}%`;
  }, 1500);
}

function reflectionKey(matchId, puuid) { return `${matchId}:${puuid}`; }

async function ensureReflection(matchId, puuid) {
  const key = reflectionKey(matchId, puuid);
  if (reflectionUi.cache.has(key)) return;
  reflectionUi.cache.set(key, await getJSON(
    `/api/reflections?match_id=${encodeURIComponent(matchId)}&puuid=${encodeURIComponent(puuid)}`));
}

function reflectionTagCount(matchId, puuid) {
  const cached = reflectionUi.cache.get(reflectionKey(matchId, puuid));
  return cached ? cached.tags.length : 0;
}

async function putReflection(matchId, puuid, fields) {
  return fetch(`/api/reflections/${encodeURIComponent(matchId)}/${encodeURIComponent(puuid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
}

function reflectionSection(matchId, puuid, game) {
  const key = reflectionKey(matchId, puuid);
  const reflection = reflectionUi.cache.get(key);
  if (!reflection) {
    return `<div class="reflection-section" data-match="${escapeHtml(matchId)}" data-puuid="${escapeHtml(puuid)}">
      <h5>Reflection</h5><p class="muted">Loading…</p></div>`;
  }
  const tags = reflection.tags;
  // chosen tags and suggestions are separate rows: a suggestion sitting in the
  // same row as your picks reads as already-chosen, which it isn't
  const chosen = tags.length
    ? tags.map((t) => `<button type="button" class="chip chip-main reflection-tag"
        data-tag="${escapeHtml(t)}" aria-pressed="true"
        title="Remove this tag">${escapeHtml(t)} ✕</button>`).join("")
    : `<span class="muted reflection-empty">No tags on this game yet.</span>`;
  const suggestions = reflectionSuggestions(game).filter((t) => !tags.includes(t));
  const suggested = suggestions.length
    ? `<div class="reflection-suggest">
        <span class="muted reflection-suggest-label">Suggested for this game</span>
        ${suggestions.map((t) => `<button type="button" class="chip chip-suggest reflection-tag"
          data-tag="${escapeHtml(t)}" aria-pressed="false"
          title="Add this tag">+ ${escapeHtml(t)}</button>`).join("")}
      </div>`
    : "";
  const chips = chosen;
  const editing = reflectionUi.editingNote.has(key);
  const noteBody = editing
    ? `<div class="mu-notes">
        <label class="filter-label">Note (Markdown)</label>
        <textarea class="reflection-note-input" rows="4"
          placeholder="Optional freeform note — what happened, what to do differently…">${escapeHtml(reflection.note)}</textarea>
        <div class="session-actions">
          <button type="button" class="preset reflection-note-save">Save</button>
          <button type="button" class="preset reflection-note-cancel">Cancel</button>
          <span class="muted reflection-note-status"></span>
        </div>
      </div>`
    : `<div class="reflection-note-view">
        <div class="md-body">${reflection.note ? renderNotes(reflection.note) : `<p class="muted">No note yet.</p>`}</div>
        <button type="button" class="preset icon-btn reflection-note-edit" title="Edit reflection note" aria-label="Edit reflection note">✎</button>
      </div>`;
  return `<div class="reflection-section" data-match="${escapeHtml(matchId)}" data-puuid="${escapeHtml(puuid)}">
    <h5>Reflection</h5>
    <div class="chip-box reflection-tags">
      ${chips}
      <input type="text" class="chip-input reflection-tag-input" placeholder="+ your own tag (Enter)">
    </div>
    ${suggested}
    <span class="muted reflection-tag-status"></span>
    ${noteBody}
  </div>`;
}

// reload(matchId, puuid): async callback the caller supplies to refetch that
// game's reflection into its own cache and re-render its view.
// rerender(): cheap re-render of the caller's view without refetching — used
// for opening/closing the note editor.
function wireReflectionSection(container, reload, rerender) {
  container.querySelectorAll(".reflection-tags").forEach((box) =>
    box.addEventListener("click", (e) => {
      if (e.target === box) box.querySelector(".reflection-tag-input")?.focus();
    }));
  container.querySelectorAll(".reflection-tag").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const section = btn.closest(".reflection-section");
      const { match: matchId, puuid } = section.dataset;
      const current = reflectionUi.cache.get(reflectionKey(matchId, puuid)) || { tags: [], note: "" };
      const tag = btn.dataset.tag;
      const tags = current.tags.includes(tag)
        ? current.tags.filter((t) => t !== tag) : [...current.tags, tag];
      const response = await putReflection(matchId, puuid, { tags });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        section.querySelector(".reflection-tag-status").textContent =
          body.detail || `error ${response.status}`;
        return;
      }
      await reload(matchId, puuid);
    }));
  container.querySelectorAll(".reflection-tag-input").forEach((input) =>
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter" && e.key !== ",") return;
      e.preventDefault();
      const value = input.value.replace(",", "").trim();
      if (!value) return;
      const section = input.closest(".reflection-section");
      const { match: matchId, puuid } = section.dataset;
      const current = reflectionUi.cache.get(reflectionKey(matchId, puuid)) || { tags: [], note: "" };
      if (current.tags.includes(value)) { input.value = ""; return; }
      const response = await putReflection(matchId, puuid, { tags: [...current.tags, value] });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        section.querySelector(".reflection-tag-status").textContent =
          body.detail || `error ${response.status}`;
        return;
      }
      input.value = "";
      await reload(matchId, puuid);
    }));
  container.querySelectorAll(".reflection-note-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      const section = btn.closest(".reflection-section");
      reflectionUi.editingNote.add(reflectionKey(section.dataset.match, section.dataset.puuid));
      rerender();
    }));
  container.querySelectorAll(".reflection-note-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      const section = btn.closest(".reflection-section");
      reflectionUi.editingNote.delete(reflectionKey(section.dataset.match, section.dataset.puuid));
      rerender();
    }));
  container.querySelectorAll(".reflection-note-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const section = btn.closest(".reflection-section");
      const { match: matchId, puuid } = section.dataset;
      const note = section.querySelector(".reflection-note-input").value;
      const status = section.querySelector(".reflection-note-status");
      const response = await putReflection(matchId, puuid, { note });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      reflectionUi.editingNote.delete(reflectionKey(matchId, puuid));
      await reload(matchId, puuid);
    }));
}

// ---------- coaching sessions: panes + the add/edit popup ----------

async function ensureCoaches() {
  try {
    sessionUi.coaches = (await getJSON("/api/coaches")).coaches || [];
  } catch { sessionUi.coaches = []; }
  return sessionUi.coaches;
}

// `session` = editing an existing one; omitted = adding a new one
function openSessionModal(session) {
  sessionUi.modal = session
    ? { id: session.id, date: session.session_date, title: session.title,
        coach: session.coach || "", link: session.link || "", notes: session.notes || "" }
    : { id: null, date: new Date().toISOString().slice(0, 10),
        title: "", coach: "", link: "", notes: "" };
  $("#modal-overlay").classList.remove("hidden");
  renderSessionModal();
  ensureCoaches().then(renderSessionModal);   // suggestions arrive when they do
}

function renderSessionModal() {
  const m = sessionUi.modal;
  if (!m) return;
  // Suggestions are an autocomplete, not the record: the ✕ only stops offering
  // a name, it never touches the sessions that already name them.
  const chips = sessionUi.coaches.length
    ? `<div class="coach-chips">
        <span class="muted coach-chips-label">Previously:</span>
        ${sessionUi.coaches.map((name) => `
        <span class="chip coach-chip ${name === m.coach ? "chip-main" : "chip-plain"}">
          <button type="button" class="coach-pick" data-name="${escapeHtml(name)}"
            title="Use this coach">${escapeHtml(name)}</button>
          <button type="button" class="coach-forget" data-name="${escapeHtml(name)}"
            title="Stop suggesting ${escapeHtml(name)} (sessions keep it)"
            aria-label="Stop suggesting ${escapeHtml(name)}">×</button>
        </span>`).join("")}</div>`
    : "";
  $("#modal-box").innerHTML = `<div class="session-modal">
    <div class="section-head">
      <h3>${m.id ? "Edit coaching session" : "New coaching session"}</h3>
      <button class="preset icon-btn" id="modal-close" title="Close" aria-label="Close">✕</button>
    </div>
    <div class="sm-grid">
      <label class="filter-label" for="sm-date">Date</label>
      <input type="date" id="sm-date" class="sm-date" value="${escapeHtml(m.date)}"
        ${m.id ? "disabled title='A session is keyed by its date'" : ""}>

      <label class="filter-label" for="sm-title">Title</label>
      <input type="text" id="sm-title" value="${escapeHtml(m.title)}"
        placeholder="e.g. wave management">

      <label class="filter-label" for="sm-coach">Coach</label>
      <div class="sm-coach-field">
        <input type="text" id="sm-coach" class="sm-coach" value="${escapeHtml(m.coach)}"
          placeholder="optional">
        ${chips}
      </div>

      <label class="filter-label" for="sm-link">Recording</label>
      <input type="url" id="sm-link" value="${escapeHtml(m.link || "")}"
        placeholder="optional — link to the VOD (weteachleague, YouTube…)">

      <label class="filter-label" for="sm-notes">Notes</label>
      <div class="sm-notes-field">
        <textarea id="sm-notes" rows="12"
          placeholder="What was covered, what to work on…">${escapeHtml(m.notes)}</textarea>
        <span class="muted sm-hint">Markdown supported</span>
      </div>
    </div>
    <div class="sm-actions">
      <span class="muted" id="sm-status"></span>
      <button class="preset" id="sm-cancel">Cancel</button>
      <button class="btn-primary" id="sm-save">${m.id ? "Save session" : "Add session"}</button>
    </div>
  </div>`;
  wireSessionModal();
}

function readSessionModal() {
  const m = sessionUi.modal;
  m.title = $("#sm-title").value;
  m.coach = $("#sm-coach").value;
  m.link = $("#sm-link").value;
  m.notes = $("#sm-notes").value;
  if (!m.id) m.date = $("#sm-date").value;
}

function wireSessionModal() {
  const box = $("#modal-box");
  box.querySelector("#modal-close").addEventListener("click", closeModal);
  const date = box.querySelector("#sm-date");
  if (date && !date.disabled) {
    // clicking the field pops the calendar, not just the little icon…
    date.addEventListener("focus", () => { try { date.showPicker(); } catch { /* unsupported */ } });
    date.addEventListener("click", () => { try { date.showPicker(); } catch { /* unsupported */ } });
    // …and once a date is chosen the cursor lands where you'd type next
    date.addEventListener("change", () => box.querySelector("#sm-title").focus());
    if (!sessionUi.modal.id) requestAnimationFrame(() => date.focus());
  }
  box.querySelector("#sm-cancel").addEventListener("click", closeModal);
  box.querySelectorAll(".coach-pick").forEach((btn) =>
    btn.addEventListener("click", () => {
      readSessionModal();
      sessionUi.modal.coach = btn.dataset.name;
      renderSessionModal();
    }));
  box.querySelectorAll(".coach-forget").forEach((btn) =>
    btn.addEventListener("click", async () => {
      readSessionModal();
      await fetch(`/api/coaches/${encodeURIComponent(btn.dataset.name)}`, { method: "DELETE" });
      await ensureCoaches();
      renderSessionModal();
    }));
  box.querySelector("#sm-save").addEventListener("click", async () => {
    readSessionModal();
    const m = sessionUi.modal;
    const status = $("#sm-status");
    status.textContent = "";
    const response = m.id
      ? await fetch(`/api/sessions/${m.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: m.title, coach: m.coach, link: m.link,
                                 notes: m.notes }) })
      : await fetch("/api/sessions", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ date: m.date, title: m.title, coach: m.coach,
                                 link: m.link, notes: m.notes }) });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || `error ${response.status}`;
      return;   // keep what they typed
    }
    sessionUi.modal = null;
    closeModal();
    loadProgress();
  });
}

async function ensureSessionClips(id) {
  if (sessionUi.clips.has(id)) return;
  sessionUi.clips.set(id, await getJSON(`/api/clips?owner_type=session&owner_id=${id}`));
}

async function unionFilterOptions() {
  // server unions across the selected accounts (all tracked when unscoped)
  const opts = await getJSON(`/api/filters?${accountParams()}`);
  return { champions: opts.champions, queues: opts.queues };
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
  const params = accountParams();
  if (state.progressChampion) params.set("champion", state.progressChampion);
  if (state.progressQueue) params.set("queue", state.progressQueue);
  if (state.progressSide) params.set("side", state.progressSide);
  const [segments, sessionRows] = await Promise.all([
    getJSON(`/api/stats/progress?${params}`),
    getJSON("/api/sessions"),
  ]);
  // winrate in percentage points for delta display
  segments.forEach((s) => { s.winrate_pp = s.winrate == null ? null : 100 * s.winrate; });
  segmentUi.cache.clear(); // filters or data changed; refetch game lists on expand
  renderProgress(segments);
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

const WIDE_VIEWS = new Set(["matchups", "progress", "trends", "blocks"]);

// The ONE place the top nav is defined: sections in bar order, each with its
// views in sub-bar order. Adding a view = one line here (plus its <div
// id="<key>-view"> and an init call in setMainView) — no markup to touch.
// Settings is deliberately absent: it's a place you enter and leave, not a
// mode you work in, so it stays the ⚙ icon.
const NAV_SECTIONS = [
  { key: "analyze", label: "Analyze", views: ["overview", "matchups", "trends"] },
  { key: "coach", label: "Coach", views: ["progress", "blocks", "series", "pool"] },
  { key: "prepare", label: "Prepare",
    views: ["guide", "tiers", "research", "players"] },
];
const VIEW_LABELS = {
  overview: "Overview", matchups: "Matchups", trends: "Trends",
  progress: "Coaching progress", blocks: "Blocks", series: "Series",
  pool: "Champion pool",
  guide: "Playbook", tiers: "Tier list", research: "Research",
  players: "Research players",
};
const ALL_VIEWS = NAV_SECTIONS.flatMap((s) => s.views);

function sectionOf(view) {
  return NAV_SECTIONS.find((s) => s.views.includes(view)) || null;
}

function viewHidden(view) {
  return (state.hiddenViews || []).includes(view);
}

// a section disappears entirely once all its views are hidden in Settings
function sectionVisible(section) {
  return section.views.some((v) => !viewHidden(v));
}

function renderNav() {
  const active = sectionOf(state.mainView);
  $("#main-view-toggle").innerHTML = NAV_SECTIONS
    .filter(sectionVisible)
    .map((s) => `<button type="button" role="tab" data-section="${s.key}"
      class="${active && active.key === s.key ? "active" : ""}">${s.label}</button>`).join("");
  // the sub-bar shows the ACTIVE section's views; Settings has no section, so
  // it keeps showing the section you came from rather than emptying the bar
  const shown = active || sectionOf(state.lastView) || NAV_SECTIONS[0];
  $("#sub-view-toggle").innerHTML = shown.views.filter((v) => !viewHidden(v))
    .map((v) => `<button type="button" role="tab" data-view="${v}"
      class="${state.mainView === v ? "active" : ""}">${VIEW_LABELS[v]}</button>`).join("");
}

// entering a section lands on the view you last used there, so switching back
// and forth doesn't dump you on its first tab every time
function sectionEntryView(section) {
  const remembered = localStorage.getItem(`cp-nav-last-${section.key}`);
  if (remembered && section.views.includes(remembered) && !viewHidden(remembered)) {
    return remembered;
  }
  return section.views.find((v) => !viewHidden(v)) || section.views[0];
}

function setMainView(view) {
  state.mainView = view;
  const section = sectionOf(view);
  if (section) {
    state.lastView = view;
    localStorage.setItem(`cp-nav-last-${section.key}`, view);
  }
  if (history.replaceState) {
    history.replaceState(null, "", view === "overview" ? "#" : `#${view}`);
  }
  for (const v of [...ALL_VIEWS, "settings"]) {
    $(`#${v}-view`).classList.toggle("hidden", view !== v);
  }
  $("#nav-settings").classList.toggle("active", view === "settings");
  renderNav();
  // Column-picker views can get very wide (many metric columns); let them use
  // the full window width instead of the centred reading column, so the user
  // can widen the window to reveal columns rather than scroll (issue #8).
  document.body.classList.toggle("wide-view", WIDE_VIEWS.has(view));
  if (view === "matchups") initMatchups();
  if (view === "progress") loadProgressFilterOptions().then(loadProgress);
  if (view === "trends") initTrends();
  if (view === "blocks") initBlocks();
  if (view === "guide") initGuide();
  if (view === "research") initResearch();
  if (view === "players") initPlayers();
  if (view === "tiers") initTiers();
  if (view === "series") initSeriesView();
  if (view === "pool") initPool();
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
       <button class="chip-x" data-account="${escapeHtml(a)}" title="Remove from the tracked list (on Save)"
         aria-label="Remove ${escapeHtml(a)}">×</button>
       <button class="chip-del" data-account="${escapeHtml(a)}"
         title="Delete this account and its crawled data from the database"
         aria-label="Delete ${escapeHtml(a)} from the database">🗑</button></span>`).join(""));
  box.querySelectorAll(".chip-x").forEach((btn) =>
    btn.addEventListener("click", () => {
      settingsUi.accounts = settingsUi.accounts.filter((a) => a !== btn.dataset.account);
      renderAccountChips();
    }));
  box.querySelectorAll(".chip-del").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const account = btn.dataset.account;
      if (!confirm(`Delete ${account} and all its crawled data (matches, coaching `
        + `metrics, runes, rank history) from the database?\n\nYour blocks, sessions and `
        + `notes are kept. You can re-add and re-crawl the account later.`)) return;
      const res = await fetch("/api/accounts", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account }),
      });
      const bodyj = await res.json().catch(() => ({}));
      if (!res.ok) {
        $("#settings-status").textContent = bodyj.detail || `error ${res.status}`;
        return;
      }
      settingsUi.accounts = bodyj.accounts || settingsUi.accounts.filter((a) => a !== account);
      renderAccountChips();
      $("#settings-status").textContent =
        `deleted ${account} — ${bodyj.players_deleted} player record(s) purged`;
      init(false); // stats/account tabs change now the data's gone
    }));
}

function rgbToHex(colorString) {
  const nums = colorString.match(/\d+/g);
  if (!nums) return colorString.trim();
  return "#" + nums.slice(0, 3).map((n) => (+n).toString(16).padStart(2, "0")).join("");
}

function applyAppearance(data) {
  if (data.ui_opacity !== undefined) {
    document.documentElement.style.setProperty("--ui-opacity", data.ui_opacity / 100);
  }
  if ("accent_color" in data) {
    if (data.accent_color) {
      document.documentElement.style.setProperty("--series-1", data.accent_color);
    } else {
      document.documentElement.style.removeProperty("--series-1");
    }
  }
  if ("background_image" in data) {
    const bg = $("#bg-image");
    if (data.background_image) {
      bg.style.backgroundImage = `url(/api/settings/background/file?v=${Date.now()})`;
      bg.classList.add("active");
    } else {
      bg.style.backgroundImage = "";
      bg.classList.remove("active");
    }
  }
}

// comparison is opt-in: the buttons that open it only exist when it's on
function applyComparisonEnabled() {
  const btn = $("#progress-compare");
  if (btn) btn.classList.toggle("hidden", !state.enableComparison);
}

function applyHiddenViews(hidden) {
  state.hiddenViews = hidden || [];
  if (viewHidden(state.mainView)) {
    setMainView(ALL_VIEWS.find((view) => !viewHidden(view)) || "settings");
  } else {
    renderNav();  // a hidden view (or an emptied section) drops out of the bars
  }
}

// champion pool flattened to one priority-ordered list (main blind first,
// then core, then counters — pool order is user-set by dragging chips).
// Cached; blocks.js's savePool resets it.
async function poolChampionOrder() {
  if (state.poolOrder) return state.poolOrder;
  try {
    const pool = await getJSON("/api/pool");
    state.poolOrder = [...new Set([
      ...(pool.main_blind ? [pool.main_blind] : []), ...pool.core, ...pool.counter])];
  } catch {
    state.poolOrder = [];
  }
  return state.poolOrder;
}

// full-roster <select> options: the champion pool (in pool order) grouped on
// top, then everyone else alphabetically by display name
async function championOptions(selected, emptyLabel) {
  await loadChampionRoster(); // blocks.js — cached after the first call
  const pool = (await poolChampionOrder()).filter((c) => roster.nameById.has(c));
  const poolSet = new Set(pool);
  const rest = [...roster.nameById.keys()].filter((c) => !poolSet.has(c)).sort(
    (a, b) => champDisplay(a).localeCompare(champDisplay(b)));
  const opt = (c) =>
    `<option value="${c}" ${c === selected ? "selected" : ""}>${escapeHtml(champDisplay(c))}</option>`;
  const empty = emptyLabel === undefined ? "" : `<option value="">${emptyLabel}</option>`;
  if (!pool.length) return empty + rest.map(opt).join("");
  return empty
    + `<optgroup label="Champion Pool">${pool.map(opt).join("")}</optgroup>`
    + `<optgroup label="All champions">${rest.map(opt).join("")}</optgroup>`;
}

// legacy matchup notes (pre-champ-guide, my_champion='') — Settings offers to
// migrate them under one of your champions or delete them; the section only
// shows while such rows exist
async function refreshLegacySection() {
  const section = $("#legacy-notes-section");
  let info;
  try {
    info = await getJSON("/api/matchups/legacy-notes");
  } catch {
    return;
  }
  section.classList.toggle("hidden", !info.count);
  if (!info.count) return;
  $("#legacy-notes-summary").textContent =
    `${info.count} matchup note(s) from before the champ-guide update — ` +
    `${Object.keys(info.notes).map(displayName).join(", ")} — aren't tied to one of ` +
    `your champions, so they don't appear in the Playbook. Assign them to a ` +
    `champion, or delete them.`;
  const select = $("#legacy-migrate-champion");
  if (!select.options.length) {
    select.innerHTML = await championOptions((await poolChampionOrder())[0] || "");
  }
}

// ---------- comparison ("research") players (own view under Prepare) ----------
// The feature TOGGLE stays in Settings (it's configuration); the players
// themselves are a working list you revise, so they get their own view next to
// Research, whose games they are.
const playersUi = { wired: false };

async function initPlayers() {
  // Settings may never have been opened this session, so don't rely on it
  // having cached the flag or filled the server dropdown
  if (state.enableComparison === undefined || !$("#comparison-add-platform").options.length) {
    try {
      const data = await getJSON("/api/settings");
      state.enableComparison = Boolean(data.enable_player_comparison);
      fillPlatformSelect($("#comparison-add-platform"), data, data.platform);
    } catch { state.enableComparison = state.enableComparison || false; }
  }
  const off = !state.enableComparison;
  $("#players-disabled").classList.toggle("hidden", !off);
  $("#players-card").classList.toggle("hidden", off);
  if (off) return;
  await loadComparisonPlayers();
  if (playersUi.wired) return;
  playersUi.wired = true;
  $("#comparison-add").addEventListener("click", addComparisonPlayer);
  $("#comparison-refresh-all").addEventListener("click", refreshAllComparisonPlayers);
  $("#comparison-add-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addComparisonPlayer(); }
  });
  $("#players-settings-link").addEventListener("click", () => setMainView("settings"));
}

async function loadComparisonPlayers() {
  const list = $("#comparison-players-list");
  if (!list) return;
  let data;
  try { data = await getJSON("/api/comparison-players"); }
  catch { list.innerHTML = ""; return; }
  // a poll landing mid-edit must not blow away a half-typed note (the list is
  // re-rendered wholesale); skip this cycle, the next one repaints it
  const active = document.activeElement;
  if (!(active && active.classList && active.classList.contains("cmp-note"))) {
    renderComparisonPlayers(data.players || [], data.fetching || {});
  }
  // a background fetch is running — poll until it finishes, updating counts
  const status = $("#comparison-status");
  if (data.fetching && data.fetching.running) {
    if (status) status.textContent = data.fetching.message || "fetching games…";
    clearTimeout(state.comparisonPoll);
    state.comparisonPoll = setTimeout(loadComparisonPlayers, 2500);
  } else if (data.fetching && data.fetching.error) {
    if (status) status.textContent = `fetch failed — ${data.fetching.error}`;
  } else if (data.fetching && data.fetching.message && data.fetching.message !== "idle") {
    if (status) status.textContent = data.fetching.message;
  }
}

function renderComparisonPlayers(players, fetching = {}) {
  const list = $("#comparison-players-list");
  if (!list) return;
  const busy = Boolean(fetching.running);
  list.innerHTML = players.length
    ? players.map((p) => `
      <div class="comparison-player" data-puuid="${p.puuid}">
        <label class="comparison-enable" title="Show this player in the guide comparison">
          <input type="checkbox" class="cmp-enable" ${p.enabled ? "checked" : ""}></label>
        <span class="comparison-name">${escapeHtml(p.game_name)}<span class="muted">#${escapeHtml(p.tag_line)}</span></span>
        <input class="chip-input cmp-note" type="text" maxlength="200" value="${escapeHtml(p.note || "")}"
          placeholder="note…" spellcheck="false" autocomplete="off"
          title="Your note about this player — what they main, why you're watching them">
        <span class="muted comparison-games">${p.platform ? (PLATFORM_LABELS[p.platform] || p.platform.toUpperCase()) + " · " : ""}${p.games} game${p.games === 1 ? "" : "s"}${busy && fetching.puuid === p.puuid ? " · fetching…" : ""}</span>
        <button class="preset cmp-more" type="button" ${busy ? "disabled" : ""}
          title="Fetch &amp; store more of this player's games (deeper history)">Fetch more</button>
        <button class="preset icon-btn cmp-remove" type="button" title="Remove">✕</button>
      </div>`).join("")
    : `<p class="muted">No comparison players yet — add as many as you like.</p>`;
  $("#comparison-add-input").disabled = busy;
  $("#comparison-add").disabled = busy;
  $("#comparison-refresh-all").disabled = busy || !players.length;
  list.querySelectorAll(".cmp-enable").forEach((cb) =>
    cb.addEventListener("change", () =>
      toggleComparisonPlayer(cb.closest(".comparison-player").dataset.puuid, cb.checked)));
  list.querySelectorAll(".cmp-note").forEach((input) => {
    // change fires on blur and on Enter, so one listener covers both
    input.addEventListener("change", () =>
      setComparisonNote(input.closest(".comparison-player").dataset.puuid, input.value));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  });
  list.querySelectorAll(".cmp-more").forEach((btn) =>
    btn.addEventListener("click", () =>
      comparisonFetchMore(btn.closest(".comparison-player").dataset.puuid, btn)));
  list.querySelectorAll(".cmp-remove").forEach((btn) =>
    btn.addEventListener("click", () =>
      removeComparisonPlayer(btn.closest(".comparison-player").dataset.puuid)));
}

async function addComparisonPlayer() {
  const input = $("#comparison-add-input");
  const riotId = input.value.trim();
  if (!riotId) return;
  const status = $("#comparison-status");
  status.textContent = `looking up ${riotId}…`;
  const res = await fetch("/api/comparison-players", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ riot_id: riotId, platform: $("#comparison-add-platform").value }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) {
    input.value = "";
    status.textContent = `added ${body.game_name}#${body.tag_line} — fetching games in the background…`;
    loadComparisonPlayers(); // picks up the running fetch and polls it
  } else {
    status.textContent = body.detail || `error ${res.status}`;
  }
}

async function comparisonFetchMore(puuid, btn) {
  const status = $("#comparison-status");
  btn.disabled = true; btn.textContent = "fetching…";
  const res = await fetch(`/api/comparison-players/${encodeURIComponent(puuid)}/fetch-more`,
    { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) status.textContent = body.detail || `error ${res.status}`;
  loadComparisonPlayers(); // background fetch started — poll for progress
}

async function toggleComparisonPlayer(puuid, enabled) {
  await fetch(`/api/comparison-players/${encodeURIComponent(puuid)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

async function setComparisonNote(puuid, note) {
  const status = $("#comparison-status");
  const res = await fetch(`/api/comparison-players/${encodeURIComponent(puuid)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    status.textContent = body.detail || `note not saved — error ${res.status}`;
  }
}

// every player in one background job — one at a time server-side, since only
// one Riot fetch may run at a time
async function refreshAllComparisonPlayers() {
  const status = $("#comparison-status");
  const count = document.querySelectorAll("#comparison-players-list .comparison-player").length;
  if (!count) return;
  if (!confirm(`Fetch new games for all ${count} research player${count === 1 ? "" : "s"}? `
             + "Riot's API is rate limited, so this runs in the background and can take a while."))
    return;
  status.textContent = "starting…";
  const res = await fetch("/api/comparison-players/refresh-all", { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) status.textContent = body.detail || `error ${res.status}`;
  loadComparisonPlayers(); // background fetch started — poll for progress
}

async function removeComparisonPlayer(puuid) {
  if (!confirm("Remove this comparison player? Their fetched games stay in the db.")) return;
  await fetch(`/api/comparison-players/${encodeURIComponent(puuid)}`, { method: "DELETE" });
  loadComparisonPlayers();
}

// shared by Settings and the Research players view — either can be the first
// one opened, so neither may assume the other filled the dropdown
function fillPlatformSelect(select, data, selected) {
  const platforms = [...data.platforms].sort((a, b) =>
    PLATFORM_ORDER.indexOf(a) - PLATFORM_ORDER.indexOf(b));
  select.innerHTML = platforms.map((p) =>
    `<option value="${p}" ${p === selected ? "selected" : ""}>${PLATFORM_LABELS[p] || p.toUpperCase()}</option>`).join("");
}

async function initSettings() {
  await loadChampionRoster(); // populates the shared #champ-list datalist
  const data = await getJSON("/api/settings");
  $("#setting-key").value = data.riot_api_key;
  fillPlatformSelect($("#setting-platform"), data, data.platform);
  fillPlatformSelect($("#comparison-add-platform"), data, data.platform); // default to yours
  settingsUi.accounts = data.accounts;
  settingsUi.wasUnconfigured = !data.configured;
  renderAccountChips();
  document.querySelectorAll(".view-toggle-cb").forEach((cb) => {
    cb.checked = !(data.hidden_views || []).includes(cb.value);
  });
  $("#setting-auto-crawl").value = data.auto_crawl_hours;
  $("#setting-block-size").value = data.block_size;
  $("#setting-block-gap").value = data.block_gap_hours;
  $("#setting-block-gap-confirm").checked = Boolean(data.block_gap_confirm);
  $("#setting-block-series").checked = Boolean(data.block_series_enabled);
  $("#setting-date-format").value = data.date_format || "iso";
  $("#setting-main-role").innerHTML = roleSettingOptions(data.main_role || "");
  $("#setting-secondary-role").innerHTML = roleSettingOptions(data.secondary_role || "");
  $("#setting-runes-mode").value = data.runes_mode || "matchup";
  state.runesMode = data.runes_mode || "matchup";
  $("#setting-obs-host").value = data.obs_host || "";
  $("#setting-obs-port").value = data.obs_port || "";
  $("#setting-obs-password").value = data.obs_password || "";
  $("#setting-ascent-db").value = data.ascent_db_path || "";
  $("#ascent-detected").textContent = data.ascent_db_detected || "none found";
  $("#setting-youtube-secrets").value = data.youtube_client_secrets || "";
  $("#setting-youtube-privacy").value = data.youtube_privacy || "private";
  state.youtubePrivacy = data.youtube_privacy || "private";
  state.youtubeReady = Boolean(data.youtube_ready);
  $("#setting-enable-comparison").checked = Boolean(data.enable_player_comparison);
  $("#comparison-card").classList.toggle("hidden", !data.enable_player_comparison);
  state.enableComparison = Boolean(data.enable_player_comparison);
  applyComparisonEnabled();
  $("#setting-hide-rank").checked = Boolean(data.hide_my_rank);
  await loadChampionRoster(); // legacy migrate select needs display names
  refreshLegacySection();
  $("#setting-accent-color").value = data.accent_color
    || rgbToHex(getComputedStyle(document.documentElement).getPropertyValue("--series-1"));
  $("#setting-accent-reset").classList.toggle("hidden", !data.accent_color);
  $("#setting-ui-opacity").value = data.ui_opacity;
  $("#setting-ui-opacity-value").textContent = `${data.ui_opacity}%`;
  $("#setting-bg-remove").classList.toggle("hidden", !data.background_image);
  applyAppearance(data);
  $("#settings-banner").classList.toggle("hidden", data.configured);
  if (settingsUi.wired) return;
  settingsUi.wired = true;
  $("#settings-pool-link").addEventListener("click", () => setMainView("pool"));
  // the players themselves live on their own view (initPlayers); the toggle
  // stays here because it enables the feature, and the view follows it
  $("#settings-players-link").addEventListener("click", () => setMainView("players"));
  $("#sync-recordings").addEventListener("click", syncRecordings);
  $("#obs-test").addEventListener("click", testObsConnection);
  $("#setting-enable-comparison").addEventListener("change", (e) => {
    $("#comparison-card").classList.toggle("hidden", !e.target.checked);
    state.enableComparison = e.target.checked;
  });
  $("#import-all-btn").addEventListener("click", async () => {
    const status = $("#import-all-status");
    const file = $("#import-all-file").files[0];
    if (!file) { status.textContent = "choose a .zip file first"; return; }
    status.textContent = "checking…";
    const previewData = new FormData();
    previewData.append("file", file);
    const preview = await fetch("/api/import-all/preview", { method: "POST", body: previewData });
    const previewBody = await preview.json().catch(() => ({}));
    if (!preview.ok) {
      status.textContent = previewBody.detail || `error ${preview.status}`;
      return;
    }
    if (previewBody.conflicts.length) {
      status.textContent = `Can't import — would overwrite: ${previewBody.conflicts.slice(0, 5).join(", ")}` +
        `${previewBody.conflicts.length > 5 ? "…" : ""}`;
      return;
    }
    const c = previewBody.counts;
    const summary = [
      c.sessions && `${c.sessions} session(s)`, c.blocks && `${c.blocks} block(s)`,
      c.matchup_notes && `${c.matchup_notes} matchup guide(s)`,
      c.champion_notes && `${c.champion_notes} champion note(s)`,
      c.item_builds && `${c.item_builds} item build(s)`,
      c.research_entries && `${c.research_entries} research entr(y/ies)`,
      c.clips && `${c.clips} clip(s)`,
    ].filter(Boolean).join(", ");
    if (!confirm(`Import ${summary || "this backup"}?`)) { status.textContent = "cancelled"; return; }
    status.textContent = "importing…";
    const importData = new FormData();
    importData.append("file", file);
    const response = await fetch("/api/import-all", { method: "POST", body: importData });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      status.textContent = body.detail || `error ${response.status}`;
      return;
    }
    status.textContent = "imported ✓ — reloading…";
    setTimeout(() => location.reload(), 1000);
  });
  $("#setting-accent-color").addEventListener("input", (e) => {
    document.documentElement.style.setProperty("--series-1", e.target.value);
    $("#setting-accent-reset").classList.remove("hidden");
  });
  $("#setting-accent-reset").addEventListener("click", () => {
    document.documentElement.style.removeProperty("--series-1");
    $("#setting-accent-color").value =
      rgbToHex(getComputedStyle(document.documentElement).getPropertyValue("--series-1"));
    $("#setting-accent-reset").classList.add("hidden");
  });
  $("#setting-ui-opacity").addEventListener("input", (e) => {
    $("#setting-ui-opacity-value").textContent = `${e.target.value}%`;
    document.documentElement.style.setProperty("--ui-opacity", e.target.value / 100);
  });
  $("#setting-bg-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("#setting-bg-status").textContent = "uploading…";
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch("/api/settings/background", { method: "POST", body: formData });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      $("#setting-bg-status").textContent = "saved ✓";
      $("#setting-bg-remove").classList.remove("hidden");
      applyAppearance({ background_image: true });
    } else {
      $("#setting-bg-status").textContent = body.detail || `error ${response.status}`;
    }
    e.target.value = "";
  });
  $("#setting-bg-remove").addEventListener("click", async () => {
    await fetch("/api/settings/background", { method: "DELETE" });
    $("#setting-bg-remove").classList.add("hidden");
    $("#setting-bg-status").textContent = "";
    applyAppearance({ background_image: false });
  });
  $("#legacy-migrate-btn").addEventListener("click", async () => {
    const champ = $("#legacy-migrate-champion").value;
    const status = $("#legacy-notes-status");
    if (!champ) { status.textContent = "pick a champion first"; return; }
    const response = await fetch("/api/matchups/legacy-notes/migrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ my_champion: champ }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      status.textContent = body.detail || `error ${response.status}`;
      return;
    }
    status.textContent = `Moved ${body.migrated} note(s) to ${displayName(champ)}`
      + (body.skipped.length
        ? ` — skipped ${body.skipped.map(displayName).join(", ")} (${displayName(champ)} already has a guide for them)`
        : "");
    $("#settings-status").textContent = status.textContent;
    refreshLegacySection();
  });
  $("#legacy-delete-btn").addEventListener("click", async () => {
    if (!confirm("Delete these older matchup notes for good? This cannot be undone.")) return;
    const response = await fetch("/api/matchups/legacy-notes", { method: "DELETE" });
    const body = await response.json().catch(() => ({}));
    $("#settings-status").textContent = `Deleted ${body.deleted ?? 0} older matchup note(s).`;
    refreshLegacySection();
  });
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
        block_size: Math.max(1, parseInt($("#setting-block-size").value, 10) || 3),
        block_gap_hours: Math.min(168, Math.max(0, parseFloat($("#setting-block-gap").value) || 0)),
        block_gap_confirm: $("#setting-block-gap-confirm").checked,
        block_series_enabled: $("#setting-block-series").checked,
        date_format: $("#setting-date-format").value,
        main_role: $("#setting-main-role").value,
        secondary_role: $("#setting-secondary-role").value,
        runes_mode: $("#setting-runes-mode").value,
        obs_host: $("#setting-obs-host").value.trim(),
        obs_port: parseInt($("#setting-obs-port").value, 10) || 4455,
        obs_password: $("#setting-obs-password").value,
        ascent_db_path: $("#setting-ascent-db").value.trim(),
        youtube_client_secrets: $("#setting-youtube-secrets").value.trim(),
        youtube_privacy: $("#setting-youtube-privacy").value,
        enable_player_comparison: $("#setting-enable-comparison").checked,
        hide_my_rank: $("#setting-hide-rank").checked,
        ui_opacity: Math.min(100, Math.max(20, parseInt($("#setting-ui-opacity").value, 10) || 100)),
        accent_color: $("#setting-accent-reset").classList.contains("hidden")
          ? null : $("#setting-accent-color").value,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      if (Boolean(state.hideMyRank) !== Boolean(body.hide_my_rank)) {
        state.hideMyRank = body.hide_my_rank;
        init(false); // re-pull data so the redaction change applies everywhere
      }
      applyHiddenViews(body.hidden_views);
      applyAppearance(body);
      applyRoleSettings(body); // main/secondary role changed -> refilter
      refresh();
      if (state.dateFormat !== body.date_format) {
        state.dateFormat = body.date_format;
        refresh(); // re-render visible dates in the new format
      }
      const runesModeChanged = state.runesMode !== body.runes_mode;
      state.runesMode = body.runes_mode;
      if (runesModeChanged && typeof loadGuide === "function"
          && state.mainView === "guide") loadGuide();
      state.enableComparison = Boolean(body.enable_player_comparison);
      applyComparisonEnabled();
      $("#comparison-card").classList.toggle("hidden", !body.enable_player_comparison);
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
  // both nav rows re-render on every view change, so the listeners are
  // delegated to the containers rather than the buttons
  $("#main-view-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-section]");
    if (!btn) return;
    const section = NAV_SECTIONS.find((s) => s.key === btn.dataset.section);
    if (section) setMainView(sectionEntryView(section));
  });
  $("#sub-view-toggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) setMainView(btn.dataset.view);
  });
  $("#nav-settings").addEventListener("click", () => setMainView("settings"));
  // 1/2/3 jump between sections — ignored while typing so notes editors and
  // champion inputs keep working
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const el = document.activeElement;
    if (el && (el.matches("input, textarea, select") || el.isContentEditable)) return;
    if (!$("#modal-overlay").classList.contains("hidden")) return;
    const index = ["1", "2", "3"].indexOf(e.key);
    if (index === -1) return;
    const section = NAV_SECTIONS.filter(sectionVisible)[index];
    if (section) setMainView(sectionEntryView(section));
  });
  $("#progress-champion").addEventListener("change", (e) => {
    state.progressChampion = e.target.value; loadProgress();
  });
  $("#progress-queue").addEventListener("change", (e) => {
    state.progressQueue = e.target.value; loadProgress();
  });
  $("#progress-side").addEventListener("change", (e) => {
    state.progressSide = e.target.value; loadProgress();
  });
  document.querySelectorAll(".session-add-btn").forEach((btn) =>
    btn.addEventListener("click", () => openSessionModal()));

}

// ---------- patch notes ----------

async function ensureChangelog() {
  if (state.changelog) return;
  try {
    state.changelog = (await getJSON("changelog.json")).entries || [];
  } catch {
    state.changelog = [];
  }
}

async function openChangelog() {
  await ensureChangelog();
  const latestRelease = (localStorage.getItem("cp-latest-tag") || "").replace(/^v/, "");
  $("#changelog-body").innerHTML = state.changelog.map((entry) => {
    const unreleased = latestRelease && isNewerVersion(entry.version, latestRelease);
    return `<div class="changelog-entry">
      <h3>v${escapeHtml(entry.version)}
        <span class="muted">${escapeHtml(entry.date)}</span>
        ${unreleased ? `<span class="block-badge">not yet released</span>` : ""}</h3>
      <ul>${entry.items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </div>`;
  }).join("") || `<p class="muted">No entries yet.</p>`;
  $("#changelog-overlay").classList.remove("hidden");
  if (state.changelog.length) {
    localStorage.setItem("cp-changelog-seen", state.changelog[0].version);
    $("#nav-changelog").classList.remove("has-news");
  }
}

function wireChangelog() {
  $("#nav-changelog").addEventListener("click", openChangelog);
  const overlay = $("#changelog-overlay");
  $("#changelog-close").addEventListener("click", () => overlay.classList.add("hidden"));
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.classList.add("hidden");
  });
  // dot on the icon while the newest entry hasn't been opened yet
  ensureChangelog().then(() => {
    const seen = localStorage.getItem("cp-changelog-seen");
    if (state.changelog.length && state.changelog[0].version !== seen) {
      $("#nav-changelog").classList.add("has-news");
    }
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
      // refresh whichever view is active so new games appear immediately —
      // but never yank a half-written note out from under the user
      if (state.mainView === "matchups") {
        if (muState.editingNotes == null) await loadMatchups();
      } else if (state.mainView === "progress") {
        await loadProgress();
      } else if (state.mainView === "blocks") {
        if (blockState.editingNotes == null && blockState.editingLearnings == null) {
          await loadBlocks();
        }
      } else if (state.mainView === "trends") {
        await loadTrends();
      }
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
  $("#side-select").addEventListener("change", (e) => { state.side = e.target.value; refresh(); });
  $("#role-select").addEventListener("change", (e) => {
    state.roleFilter = e.target.value; syncRoleSelects(); refresh();
  });
  $("#rank-select").addEventListener("change", (e) => { state.rankTier = e.target.value; refresh(); });
  $("#min-games").addEventListener("change", (e) => { state.minGames = Math.max(1, +e.target.value || 1); refresh(); });
  // one picker for the whole progress table: base columns (default on) + metric
  // averages incl. lane deltas (default off). Expanded panels show all metrics.
  // My champions: its own column picker (all the summary's per-champion stats)
  renderColPicker($("#champion-cols"), "cp-cols-champions",
    CHAMP_ALL_COLS.filter((c) => c.key !== "champion")
      .map((c) => ({ key: c.key, label: c.label })),
    champCols, () => renderChampionTable(state.byChampion || []),
    CHAMP_DEFAULT_COLS.filter((k) => k !== "champion"));
  // Coaching progress compares TOTALS (not the per-session segments), honouring
  // whichever champion filter the view is on
  $("#progress-compare").addEventListener("click", () => openComparison({
    my: state.progressChampion || "",
    scope: state.progressChampion ? "champion" : "overall" }));
  renderColPicker($("#progress-cols"), "cp-cols-progress",
    progressAllCols().map((c) => ({ key: c.key, label: c.label })),
    progressVisibleKeys(), () => renderProgress(segmentUi.segments),
    PROGRESS_COLS.map((c) => c.key));
  $("#crawl-btn").addEventListener("click", startCrawl);
  $("#recordings-btn").addEventListener("click", rescanRecordings);
  $("#recordings-badge").addEventListener("click", () => toggleRecordingPop());
  // click-away / Esc close, like the other transient popovers
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".recordings-box")) toggleRecordingPop(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleRecordingPop(false);
  });
  $("#champion-table-toggle").addEventListener("click", () => {
    const btn = $("#champion-table-toggle");
    const table = $("#champion-table");
    const expanded = table.classList.toggle("hidden") === false;
    btn.textContent = expanded ? "▾" : "▸";
    btn.setAttribute("aria-expanded", String(expanded));
  });
}

async function loadDdragonVersion() {
  try {
    const cached = localStorage.getItem("ddragon-version");
    const cachedList = localStorage.getItem("ddragon-versions");
    const cachedAt = +localStorage.getItem("ddragon-version-at") || 0;
    if (cached && cachedList && Date.now() - cachedAt < 86_400_000) {
      state.ddragonVersion = cached;
      state.ddragonVersions = JSON.parse(cachedList);
      return;
    }
    const versions = await getJSON("https://ddragon.leagueoflegends.com/api/versions.json");
    state.ddragonVersion = versions[0];
    state.ddragonVersions = versions.slice(0, 40); // recent patches, for the guide's patch picker
    localStorage.setItem("ddragon-version", versions[0]);
    localStorage.setItem("ddragon-versions", JSON.stringify(state.ddragonVersions));
    localStorage.setItem("ddragon-version-at", String(Date.now()));
  } catch {
    state.ddragonVersion = null; // icons silently disabled offline
    state.ddragonVersions = [];
  }
}

async function init(firstLoad = true) {
  state.players = await getJSON("/api/players");
  if (firstLoad) {
    await loadDdragonVersion();
    await ensureMetricsMeta(); // metric column pickers (wired below) need it
    wireFilters();
    wireProgress();
    wireChangelog();
    pollCrawl();
    checkForUpdates();
    const settings = await getJSON("/api/settings");
    state.hideMyRank = settings.hide_my_rank;
    state.dateFormat = settings.date_format || "iso";
    state.runesMode = settings.runes_mode || "matchup";
    state.enableComparison = Boolean(settings.enable_player_comparison);
    applyComparisonEnabled();
    // recording cards read these; set at startup so the one-click upload
    // button appears without having to visit Settings first
    state.youtubePrivacy = settings.youtube_privacy || "private";
    state.youtubeReady = Boolean(settings.youtube_ready);
    applyRoleSettings(settings);
    applyHiddenViews(settings.hidden_views);
    applyAppearance(settings);
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
  if (state.accounts !== null) {
    // drop selections for accounts that no longer exist
    state.accounts = state.accounts.filter((p) =>
      state.players.some((q) => q.puuid === p));
    if (!state.accounts.length) state.accounts = null;
  }
  renderAccountSelector();
  // loadRuneTrees (guide.js) is idempotent — populates RUNE_TREES/SHARD_ROWS
  // for the recent-games rune icons on this Overview tab too, even if the
  // user never visits the Matchup guide tab
  await Promise.all([loadFilterOptions(), loadRuneTrees()]);
  await refresh();
  // every view is deep-linkable by its own key (#tiers used to be written to
  // the URL but never read back, so the Tier list couldn't be linked to)
  if (firstLoad) {
    const target = location.hash.slice(1);
    if ([...ALL_VIEWS, "settings"].includes(target) && target !== "overview") {
      setMainView(target);
    }
  }
}

init();
