"use strict";
/* Learnings view: every block's learnings in one place, for a holistic read
   rather than the working surface the Blocks view is. Reads the SAME
   /api/blocks payload blocks.js already loads (blockState.blocks/.series) —
   no endpoint of its own; the learnings-only Markdown export is the one
   server-side piece. Uses globals from app.js ($, escapeHtml, renderNotes,
   champIcon, displayName, fmtDate) and blocks.js (blockState, loadBlocks,
   blockDate, blockIndex, focusBlock, seriesById). */

const learnState = {
  wired: false,
  // "blocks" = one card per block, grouped by series; "bullets" = every
  // top-level bullet as its own line, which is how learnings are actually
  // written and the only way a year of them reads in one sitting
  mode: localStorage.getItem("cp-learn-mode") || "blocks",
  order: localStorage.getItem("cp-learn-order") || "newest",
  search: "",
  series: "",
  champion: "",
  showEmpty: false,
  editing: null,  // block id whose learnings are open for inline editing
  collapsed: new Set(JSON.parse(localStorage.getItem("cp-learn-collapsed") || "[]")),
};

function persistLearnCollapsed() {
  localStorage.setItem("cp-learn-collapsed", JSON.stringify([...learnState.collapsed]));
}

async function initLearnings() {
  if (!learnState.wired) {
    learnState.wired = true;
    $("#learn-search").addEventListener("input", (e) => {
      learnState.search = e.target.value;
      renderLearnings();
    });
    $("#learn-series").addEventListener("change", (e) => {
      learnState.series = e.target.value;
      renderLearnings();
    });
    $("#learn-champion").addEventListener("change", (e) => {
      learnState.champion = e.target.value;
      renderLearnings();
    });
    $("#learn-order").addEventListener("change", (e) => {
      learnState.order = e.target.value;
      localStorage.setItem("cp-learn-order", learnState.order);
      renderLearnings();
    });
    $("#learn-mode").querySelectorAll("button").forEach((btn) =>
      btn.addEventListener("click", () => {
        learnState.mode = btn.dataset.mode;
        localStorage.setItem("cp-learn-mode", learnState.mode);
        renderLearnings();
      }));
    $("#learn-show-empty").addEventListener("click", () => {
      learnState.showEmpty = !learnState.showEmpty;
      renderLearnings();
    });
    $("#learn-copy").addEventListener("click", copyLearningsMarkdown);
  }
  await loadBlocks();  // shared loader: also refreshes the Blocks view's data
  renderLearnings();
}

// ---------- the unit of review ----------

// Split a learnings blob into its top-level items: each column-0 bullet is one
// (with any nested/continuation lines kept under it), and prose paragraphs
// count too, so nothing written is dropped from the bullet stream.
function learningItems(text) {
  const items = [];
  let current = null;
  const flush = () => { if (current !== null && current.trim()) items.push(current.trim()); current = null; };
  for (const line of (text || "").split(/\r?\n/)) {
    if (/^[-*+]\s+/.test(line)) {
      flush();
      current = line.replace(/^[-*+]\s+/, "");
    } else if (!line.trim()) {
      flush();
    } else {
      current = current === null ? line : `${current}\n${line.replace(/^ {1,2}/, "")}`;
    }
  }
  flush();
  return items;
}

function learnMatches(text) {
  const query = learnState.search.trim().toLowerCase();
  return !query || (text || "").toLowerCase().includes(query);
}

// wrap search hits in the rendered Markdown, text nodes only so no tag is broken
function highlightLearnMatches(root) {
  const query = learnState.search.trim();
  if (!query) return;
  const needle = query.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets = [];
  while (walker.nextNode()) {
    if (walker.currentNode.nodeValue.toLowerCase().includes(needle)) targets.push(walker.currentNode);
  }
  for (const node of targets) {
    const span = document.createElement("span");
    const parts = node.nodeValue.split(new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"));
    for (const part of parts) {
      if (part.toLowerCase() === needle) {
        const mark = document.createElement("mark");
        mark.textContent = part;
        span.appendChild(mark);
      } else if (part) {
        span.appendChild(document.createTextNode(part));
      }
    }
    node.parentNode.replaceChild(span, node);
  }
}

// ---------- filtering ----------

function learnBlocks() {
  let blocks = blockState.blocks.slice();  // newest first from the API
  if (learnState.series) blocks = blocks.filter((b) => b.series_id === +learnState.series);
  if (learnState.champion) {
    blocks = blocks.filter((b) => b.games.some((g) => g.my_champion === learnState.champion));
  }
  if (learnState.order === "oldest") blocks.reverse();
  return blocks;
}

function learnChampions() {
  const champs = new Set();
  for (const block of blockState.blocks) {
    for (const game of block.games) if (game.my_champion) champs.add(game.my_champion);
  }
  return [...champs].sort((a, b) => displayName(a).localeCompare(displayName(b)));
}

function renderLearnFilters() {
  const series = $("#learn-series");
  series.innerHTML = `<option value="">All series</option>` + blockState.series
    .map((s) => `<option value="${s.id}"${+learnState.series === s.id ? " selected" : ""}>${
      escapeHtml(s.title || "Series")}</option>`).join("");
  series.parentElement.classList.toggle("hidden", !blockState.seriesEnabled
                                                  || blockState.series.length < 2);
  const champion = $("#learn-champion");
  champion.innerHTML = `<option value="">All champions</option>` + learnChampions()
    .map((c) => `<option value="${c}"${c === learnState.champion ? " selected" : ""}>${
      escapeHtml(displayName(c))}</option>`).join("");
  $("#learn-order").value = learnState.order;
  $("#learn-mode").querySelectorAll("button").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.mode === learnState.mode));
  const href = learnState.series
    ? `/api/blocks/learnings.md?series_id=${learnState.series}`
    : "/api/blocks/learnings.md";
  $("#learn-export").setAttribute("href", href);
}

// ---------- pieces ----------

function learnBlockChampions(block) {
  const champs = [...new Set(block.games.map((g) => g.my_champion).filter(Boolean))];
  return champs.map((c) => `<span class="champ-cell learn-champ" title="${escapeHtml(displayName(c))}"
    >${champIcon(c)}</span>`).join("");
}

function learnBlockHead(block) {
  const wins = block.games.filter((g) => g.win).length;
  return `<div class="learn-card-head">
    <button type="button" class="preset learn-block-link" data-id="${block.id}"
      title="Open this block in the Blocks view">${escapeHtml(block.title || blockDate(block))}
      <span class="block-index">#${blockIndex(block)}</span></button>
    ${learnBlockChampions(block)}
    <span class="muted">${block.games.length ? `${wins}–${block.games.length - wins}` : "no games"}</span>
    <span class="learn-card-actions">
      <button class="preset icon-btn learn-edit" data-id="${block.id}"
        title="Edit these learnings" aria-label="Edit learnings">✎</button>
    </span>
  </div>`;
}

// Editing happens here too — a review is when you actually want to sharpen
// what you wrote. Same click-to-edit / blur-to-save contract as the block card.
function learnBlockCard(block) {
  if (learnState.editing === block.id) {
    return `<div class="learn-card">
      ${learnBlockHead(block)}
      <textarea class="learn-textarea" data-id="${block.id}" rows="8"
        placeholder="What did this block teach you?">${escapeHtml(block.learnings)}</textarea>
      <span class="muted learnings-hint">Markdown · click away to save · Esc to cancel</span>
    </div>`;
  }
  return `<div class="learn-card${block.learnings.trim() ? "" : " learn-card-empty"}">
    ${learnBlockHead(block)}
    <div class="md-body learn-body" data-id="${block.id}" title="Click to edit">${
      block.learnings.trim() ? renderNotes(block.learnings)
      : `<p class="muted">No learnings recorded — click to write them.</p>`}</div>
  </div>`;
}

function learnSeriesGroup(series, blocks) {
  const collapsed = learnState.collapsed.has(series.id);
  const written = blocks.filter((b) => b.learnings.trim());
  const empty = blocks.length - written.length;
  const shown = learnState.showEmpty ? blocks : written;
  const head = `<div class="learn-series-head">
    <button class="preset seg-toggle learn-series-toggle" data-id="${series.id}"
      aria-expanded="${!collapsed}" title="${collapsed ? "Expand" : "Collapse"} this series">
      ${collapsed ? "▸" : "▾"}</button>
    <h3>${escapeHtml(series.title || "Series")}</h3>
    <span class="muted">${written.length} of ${blocks.length}
      ${blocks.length === 1 ? "block" : "blocks"} with learnings</span>
  </div>`;
  if (collapsed) return `<section class="learn-series">${head}</section>`;
  const goals = (series.goals || "").trim()
    ? `<div class="learn-series-frame"><h4>Goals</h4>
       <div class="md-body">${renderNotes(series.goals)}</div></div>` : "";
  const closing = (series.closing_notes || "").trim()
    ? `<div class="learn-series-frame"><h4>How it went</h4>
       <div class="md-body">${renderNotes(series.closing_notes)}</div></div>` : "";
  const body = shown.length
    ? shown.map(learnBlockCard).join("")
    : `<p class="muted">No learnings written in this series yet.</p>`;
  const hidden = !learnState.showEmpty && empty
    ? `<p class="muted learn-empty-note">${empty} ${empty === 1 ? "block" : "blocks"}
       without learnings hidden</p>` : "";
  return `<section class="learn-series">
    ${head}${goals}
    <div class="learn-cards">${body}</div>
    ${hidden}${closing}
  </section>`;
}

function learnBulletRow(block, item) {
  return `<div class="learn-bullet">
    <div class="md-body learn-bullet-body">${renderNotes(item)}</div>
    <button type="button" class="preset learn-block-link learn-bullet-source" data-id="${block.id}"
      title="Open this block in the Blocks view">${escapeHtml(block.title || blockDate(block))}
      <span class="block-index">#${blockIndex(block)}</span></button>
  </div>`;
}

// ---------- render ----------

function renderLearnings() {
  renderLearnFilters();
  const target = $("#learn-list");
  const summary = $("#learn-summary");
  if (!blockState.blocks.length) {
    summary.textContent = "";
    target.innerHTML = `<div class="muted">No blocks yet — learnings are written on a
      block once you've played its games.</div>`;
    return;
  }
  const blocks = learnBlocks();
  const written = blocks.filter((b) => b.learnings.trim());
  const totalItems = written.reduce((n, b) => n + learningItems(b.learnings).length, 0);
  summary.textContent = `${written.length} of ${blocks.length} `
    + `${blocks.length === 1 ? "block" : "blocks"} with learnings · ${totalItems} `
    + `${totalItems === 1 ? "entry" : "entries"}`;

  if (learnState.mode === "bullets") {
    const rows = blocks.flatMap((block) => learningItems(block.learnings)
      .filter(learnMatches)
      .map((item) => learnBulletRow(block, item)));
    target.innerHTML = rows.length
      ? `<div class="learn-bullets">${rows.join("")}</div>`
      : `<div class="muted">${learnState.search.trim()
        ? "Nothing written matches that search."
        : "No learnings written yet — they're edited on a block, in the Blocks view."}</div>`;
  } else {
    const matching = learnState.search.trim()
      ? blocks.filter((b) => learnMatches(b.learnings) || learnMatches(b.title))
      : blocks;
    if (blockState.seriesEnabled) {
      const bySeries = new Map();
      for (const block of matching) {
        if (!bySeries.has(block.series_id)) bySeries.set(block.series_id, []);
        bySeries.get(block.series_id).push(block);
      }
      // series order follows the block order so "oldest first" reads forwards
      const ids = [...bySeries.keys()];
      const groups = ids.map((id) => learnSeriesGroup(
        seriesById(id) || { id, title: "", goals: "", closing_notes: "" }, bySeries.get(id)));
      target.innerHTML = groups.join("") || `<div class="muted">Nothing matches those filters.</div>`;
    } else {
      const shown = learnState.showEmpty ? matching : matching.filter((b) => b.learnings.trim());
      target.innerHTML = shown.length
        ? `<div class="learn-cards">${shown.map(learnBlockCard).join("")}</div>`
        : `<div class="muted">Nothing matches those filters.</div>`;
    }
  }
  $("#learn-show-empty").textContent = learnState.showEmpty
    ? "Hide blocks without learnings" : "Show blocks without learnings";
  $("#learn-show-empty").classList.toggle("hidden", learnState.mode === "bullets");
  wireLearnings(target);
  target.querySelectorAll(".md-body").forEach(highlightLearnMatches);
}

function wireLearnings(target) {
  target.querySelectorAll(".learn-series-toggle").forEach((btn) =>
    btn.addEventListener("click", () => {
      const id = +btn.dataset.id;
      learnState.collapsed.has(id) ? learnState.collapsed.delete(id) : learnState.collapsed.add(id);
      persistLearnCollapsed();
      renderLearnings();
    }));
  target.querySelectorAll(".learn-block-link").forEach((btn) =>
    btn.addEventListener("click", () => focusBlock(+btn.dataset.id)));
  const openEditor = (id) => {
    learnState.editing = +id;
    renderLearnings();
    const input = $("#learn-list").querySelector(`.learn-textarea[data-id="${id}"]`);
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  };
  target.querySelectorAll(".learn-edit").forEach((btn) =>
    btn.addEventListener("click", () => openEditor(btn.dataset.id)));
  target.querySelectorAll(".learn-body").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;  // links inside the notes stay links
      openEditor(el.dataset.id);
    }));
  target.querySelectorAll(".learn-textarea").forEach((input) => {
    let cancelled = false;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { cancelled = true; input.blur(); }
    });
    input.addEventListener("blur", async () => {
      const id = +input.dataset.id;
      if (!cancelled) {
        await fetch(`/api/blocks/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ learnings: input.value }),
        });
        const block = blockState.blocks.find((b) => b.id === id);
        if (block) block.learnings = input.value;
      }
      cancelled = false;
      learnState.editing = null;
      renderLearnings();
    });
  });
}

// ---------- export ----------

// the same shape as /api/blocks/learnings.md, but for what's on screen: the
// filters are the review, so the copy should follow them
function learningsMarkdown() {
  const lines = ["# Block learnings", ""];
  const blocks = learnBlocks().filter((b) => b.learnings.trim() && learnMatches(b.learnings));
  let series = null;
  for (const block of blocks) {
    if (blockState.seriesEnabled && block.series_id !== series) {
      series = block.series_id;
      const row = seriesById(series);
      lines.push(`## ${(row && row.title) || "Series"}`, "");
      if (row && (row.goals || "").trim()) lines.push("**Goals**", "", row.goals.trim(), "");
    }
    const wins = block.games.filter((g) => g.win).length;
    lines.push(`### ${block.title || blockDate(block)} — #${blockIndex(block)} `
               + `(${wins}–${block.games.length - wins})`, "");
    lines.push(block.learnings.trim(), "");
  }
  return lines.join("\n").trim();
}

async function copyLearningsMarkdown() {
  const status = $("#learn-copy-status");
  try {
    await navigator.clipboard.writeText(learningsMarkdown());
    status.textContent = "copied ✓";
  } catch {
    status.textContent = "copy failed";
  }
  setTimeout(() => { status.textContent = ""; }, 2500);
}
