"use strict";
/* Trends view: per-metric time-series charts + breakdown table.
   Uses globals from app.js: state, $, getJSON, QUEUE_NAMES, escapeHtml, fmtMetric. */

const trendState = {
  wired: false,
  range: "all",
  bucket: "month",
  champion: "",
  queue: "",
  side: "", // "" all | blue | red
  data: null,
  heatmapOpen: false,
  heatmapEvents: null, // cache; cleared whenever filters change
};

// Core stats that predate the metric registry; scale maps raw -> display.
const CORE_METRICS = [
  { key: "games", label: "Games played", decimals: 0, suffix: "", direction: 0 },
  { key: "winrate", label: "Winrate", decimals: 0, suffix: "%", scale: 100, refline: 50, direction: 1 },
  { key: "kda", label: "KDA", decimals: 2, suffix: "", direction: 1 },
  { key: "cs_min", label: "CS/min", decimals: 1, suffix: "", direction: 1 },
  { key: "gold_min", label: "Gold/min", decimals: 0, suffix: "", direction: 1 },
  { key: "dmg_min", label: "DMG/min", decimals: 0, suffix: "", direction: 1 },
];

function coreValue(bucket, def) {
  const raw = bucket[def.key];
  return raw == null ? null : raw * (def.scale || 1);
}

async function initTrends() {
  if (!trendState.wired) {
    trendState.wired = true;
    document.querySelectorAll("#trend-range-presets .preset").forEach((btn) =>
      btn.addEventListener("click", () => {
        document.querySelectorAll("#trend-range-presets .preset").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        trendState.range = btn.dataset.range;
        loadTrends();
      }));
    document.querySelectorAll("#bucket-toggle .preset").forEach((btn) =>
      btn.addEventListener("click", () => {
        document.querySelectorAll("#bucket-toggle .preset").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        trendState.bucket = btn.dataset.bucket;
        loadTrends();
      }));
    $("#trend-champion").addEventListener("change", (e) => { trendState.champion = e.target.value; loadTrends(); });
    $("#trend-queue").addEventListener("change", (e) => { trendState.queue = e.target.value; loadTrends(); });
    $("#trend-role").addEventListener("change", (e) => {
      state.roleFilter = e.target.value; syncRoleSelects(); loadTrends();
    });
    $("#trend-side").addEventListener("change", (e) => { trendState.side = e.target.value; loadTrends(); });
    renderMetricColPicker($("#trend-metric-cols"), "cp-metriccols-trends", () => {  // app.js
      if (trendState.data) { renderTrendCharts(); renderTrendTable(); }
    });
    $("#heatmap-toggle").addEventListener("click", toggleHeatmap);
  }
  // options rebuild on every entry so account-scope changes are reflected
  const { champions, queues } = await unionFilterOptions();
  if (trendState.champion && !champions.includes(trendState.champion)) trendState.champion = "";
  if (trendState.queue && !queues.map(String).includes(trendState.queue)) trendState.queue = "";
  $("#trend-champion").innerHTML = `<option value="">All</option>` +
    champions.map((c) => `<option value="${c}" ${c === trendState.champion ? "selected" : ""}>${displayName(c)}</option>`).join("");
  $("#trend-queue").innerHTML = `<option value="">All</option>` +
    queues.map((q) => `<option value="${q}" ${String(q) === trendState.queue ? "selected" : ""}>${QUEUE_NAMES[q] ?? q}</option>`).join("");
  loadTrends();
}

function trendRangeParams(params) {
  if (trendState.range !== "all") params.set("range", trendState.range);
  return params;
}

async function loadTrends() {
  const seq = (trendState.seq = (trendState.seq || 0) + 1);
  const params = trendRangeParams(accountParams(new URLSearchParams({ bucket: trendState.bucket })));
  if (trendState.champion) params.set("champion", trendState.champion);
  if (trendState.queue) params.set("queue", trendState.queue);
  if (trendState.side) params.set("side", trendState.side);
  addRoleParams(params); // shared role filter (app.js)
  const data = await getJSON(`/api/stats/trends?${params}`);
  if (seq !== trendState.seq) return; // superseded by a newer load
  trendState.data = data;
  renderTrendCharts();
  renderTrendTable();
  trendState.heatmapEvents = null; // filters changed — stale
  if (trendState.heatmapOpen) loadHeatmap();
}

// ---------- charts ----------

const CHART_W = 260, CHART_H = 100;
const PAD = { l: 34, r: 8, t: 8, b: 18 };

function chartSVG(def, points) {
  // points: [{label, value}] with nulls already removed
  const values = points.map((p) => p.value);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (def.refline != null) { lo = Math.min(lo, def.refline); hi = Math.max(hi, def.refline); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  lo -= span * 0.08; hi += span * 0.08;
  const iw = CHART_W - PAD.l - PAD.r, ih = CHART_H - PAD.t - PAD.b;
  const x = (i) => PAD.l + (points.length === 1 ? iw / 2 : (i * iw) / (points.length - 1));
  const y = (v) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
  const fmt = (v) => v.toFixed(def.decimals) + (def.suffix || "");

  const line = points.length > 1
    ? `<polyline class="tl-line" points="${points.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ")}"/>`
    : "";
  const ref = def.refline != null
    ? `<line class="tl-ref" x1="${PAD.l}" x2="${CHART_W - PAD.r}" y1="${y(def.refline).toFixed(1)}" y2="${y(def.refline).toFixed(1)}"/>`
    : "";
  const dots = points.map((p, i) =>
    `<circle class="tl-dot" cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="2.5"/>
     <circle class="tl-hit" cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="9"
       data-tip="${escapeHtml(p.label)}: ${fmt(p.value)}"/>`).join("");
  const maxV = Math.max(...values), minV = Math.min(...values);
  return `<figure class="trend-chart">
    <figcaption>${def.label}</figcaption>
    <svg viewBox="0 0 ${CHART_W} ${CHART_H}" role="img" aria-label="${escapeHtml(def.label)} over time">
      <line class="tl-axis" x1="${PAD.l}" x2="${CHART_W - PAD.r}" y1="${CHART_H - PAD.b}" y2="${CHART_H - PAD.b}"/>
      <text class="tl-ylab" x="${PAD.l - 4}" y="${y(maxV) + 3}" text-anchor="end">${fmt(maxV)}</text>
      <text class="tl-ylab" x="${PAD.l - 4}" y="${y(minV) + 3}" text-anchor="end">${fmt(minV)}</text>
      ${ref}${line}${dots}
      <text class="tl-xlab" x="${PAD.l}" y="${CHART_H - 4}">${escapeHtml(points[0].label)}</text>
      <text class="tl-xlab" x="${CHART_W - PAD.r}" y="${CHART_H - 4}" text-anchor="end">${points.length > 1 ? escapeHtml(points[points.length - 1].label) : ""}</text>
    </svg>
  </figure>`;
}

function renderTrendCharts() {
  const { buckets } = trendState.data;
  const vis = visibleMetricKeys("cp-metriccols-trends");  // app.js
  const meta = trendState.data.meta.filter((m) => vis.has(m.key));
  const target = $("#trend-charts");
  if (!buckets.length) {
    target.innerHTML = `<div class="table-wrap"><div class="empty">No games match the current filters.</div></div>`;
    $("#trend-table").innerHTML = "";
    return;
  }
  const groups = [["Core", CORE_METRICS.map((d) => ({ ...d, core: true }))]];
  for (const g of [...new Set(meta.map((m) => m.group))]) {
    groups.push([g, meta.filter((m) => m.group === g)]);
  }
  target.innerHTML = groups.map(([name, defs]) => {
    const charts = defs.map((def) => {
      const points = buckets
        .map((b) => ({ label: b.bucket, value: def.core ? coreValue(b, def) : b.metrics[def.key] }))
        .filter((p) => p.value != null);
      return points.length ? chartSVG(def, points) : "";
    }).filter(Boolean).join("");
    return charts ? `<section><h3 class="trend-group">${name}</h3><div class="chart-grid">${charts}</div></section>` : "";
  }).join("");

  const tip = $("#chart-tip");
  target.querySelectorAll(".tl-hit").forEach((el) => {
    el.addEventListener("mouseenter", (e) => {
      tip.textContent = el.dataset.tip;
      tip.classList.remove("hidden");
      const r = el.getBoundingClientRect();
      tip.style.left = `${r.left + window.scrollX + 12}px`;
      tip.style.top = `${r.top + window.scrollY - 30}px`;
    });
    el.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
}

// ---------- breakdown table ----------

function renderTrendTable() {
  const { buckets } = trendState.data;
  const vis = visibleMetricKeys("cp-metriccols-trends");  // app.js
  const meta = trendState.data.meta.filter((m) => vis.has(m.key));
  const target = $("#trend-table");
  if (!buckets.length) { target.innerHTML = ""; return; }
  const groups = [...new Set(meta.map((m) => m.group))];
  const groupHeader =
    `<tr><th rowspan="2">Period</th><th colspan="${CORE_METRICS.length}" class="group-head">Core</th>` +
    groups.map((g) => `<th colspan="${meta.filter((m) => m.group === g).length}" class="group-head">${g}</th>`).join("") +
    `</tr>`;
  const labelHeader = `<tr>` +
    CORE_METRICS.map((d) => `<th>${d.label}</th>`).join("") +
    groups.map((g) => meta.filter((m) => m.group === g).map((m) => `<th>${m.label}</th>`).join("")).join("") +
    `</tr>`;
  const rows = [...buckets].reverse().map((b) => `<tr>
      <td>${b.bucket}</td>` +
    CORE_METRICS.map((d) => {
      const v = coreValue(b, d);
      return `<td>${v == null ? "–" : v.toFixed(d.decimals) + (d.suffix || "")}</td>`;
    }).join("") +
    groups.map((g) => meta.filter((m) => m.group === g)
      .map((m) => `<td>${fmtMetric(b.metrics[m.key], m)}</td>`).join("")).join("") +
    `</tr>`).join("");
  target.innerHTML = `<div class="table-wrap"><table class="trend-breakdown">
    <thead>${groupHeader}${labelHeader}</thead><tbody>${rows}</tbody></table></div>`;
}

// ---------- death map (schematic Summoner's Rift) ----------
// Riot's match-v5 timeline coordinate space, origin bottom-left (see
// metrics.parse_death_events / CLAUDE.md). Wards aren't plotted here —
// match-v5's WARD_PLACED events carry no position field.

const MAP_X_MAX = 14820, MAP_Y_MAX = 14881;
const HEATMAP_SIZE = 320, HEATMAP_PAD = 16;

function toggleHeatmap() {
  trendState.heatmapOpen = !trendState.heatmapOpen;
  const btn = $("#heatmap-toggle");
  btn.textContent = trendState.heatmapOpen ? "▾" : "▸";
  btn.setAttribute("aria-expanded", String(trendState.heatmapOpen));
  const label = trendState.heatmapOpen ? "Collapse death map" : "Expand death map";
  btn.title = label; btn.setAttribute("aria-label", label);
  $("#heatmap-body").classList.toggle("hidden", !trendState.heatmapOpen);
  if (trendState.heatmapOpen && trendState.heatmapEvents == null) loadHeatmap();
}

async function loadHeatmap() {
  const seq = (trendState.heatmapSeq = (trendState.heatmapSeq || 0) + 1);
  $("#heatmap-body").innerHTML = `<div class="empty">Loading…</div>`;
  const params = trendRangeParams(accountParams(new URLSearchParams()));
  if (trendState.champion) params.set("champion", trendState.champion);
  addRoleParams(params); // shared role filter (app.js)
  const data = await getJSON(`/api/stats/map-events?${params}`);
  if (seq !== trendState.heatmapSeq) return; // superseded by a newer load
  trendState.heatmapEvents = data.events || [];
  renderHeatmap();
}

// map-space (x: 0..MAP_X_MAX, y: 0..MAP_Y_MAX, origin bottom-left) -> SVG
// space (origin top-left) — flip y, scale both axes into the padded square.
function heatmapPoint(x, y) {
  const inner = HEATMAP_SIZE - HEATMAP_PAD * 2;
  const clamp = (v, max) => Math.min(Math.max(v, 0), max);
  const sx = HEATMAP_PAD + (clamp(x, MAP_X_MAX) / MAP_X_MAX) * inner;
  const sy = HEATMAP_PAD + (1 - clamp(y, MAP_Y_MAX) / MAP_Y_MAX) * inner;
  return [sx, sy];
}

function renderHeatmap() {
  const target = $("#heatmap-body");
  const events = trendState.heatmapEvents;
  if (events == null) return; // still loading
  const deaths = events.filter((e) => e.event_type === "death");
  if (!deaths.length) {
    target.innerHTML = `<div class="empty">No deaths recorded for the current filters.</div>`;
    return;
  }
  const S = HEATMAP_SIZE;
  const dots = deaths.map((e) => {
    const [x, y] = heatmapPoint(e.x, e.y);
    const label = `Death @ ${fmtDuration((e.timestamp_ms || 0) / 1000)}`;
    return `<circle class="heatmap-death" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4"><title>${escapeHtml(label)}</title></circle>`;
  }).join("");
  target.innerHTML = `
    <div class="muted heatmap-legend">${deaths.length} death${deaths.length === 1 ? "" : "s"} plotted</div>
    <svg class="heatmap-svg" viewBox="0 0 ${S} ${S}" role="img" aria-label="Death locations on a schematic Summoner's Rift">
      ${riftBackdrop()}
      ${dots}
    </svg>
  `;
}
