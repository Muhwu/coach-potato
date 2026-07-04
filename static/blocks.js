"use strict";
/* Block Learnings view: champion pool + auto-advancing 3-game blocks.
   Uses globals from app.js: state, $, getJSON, escapeHtml, champIcon,
   displayName, fmtDate, fmtDuration, renderNotes, unionFilterOptions. */

const blockState = { wired: false, blocks: [], blockSize: 3, editingLearnings: null };

async function initBlocks() {
  if (!blockState.wired) {
    blockState.wired = true;
    $("#pool-save").addEventListener("click", savePool);
    const { champions } = await unionFilterOptions();
    $("#champ-list").innerHTML = champions.map((c) => `<option value="${c}">`).join("");
  }
  await Promise.all([loadPool(), loadBlocks()]);
}

// ---------- champion pool ----------

async function loadPool() {
  const pool = await getJSON("/api/pool");
  $("#pool-main").value = pool.main_blind || "";
  $("#pool-core").value = pool.core.join(", ");
  $("#pool-counter").value = pool.counter.join(", ");
}

function splitChamps(value) {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

async function savePool() {
  const response = await fetch("/api/pool", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      main_blind: $("#pool-main").value.trim(),
      core: splitChamps($("#pool-core").value),
      counter: splitChamps($("#pool-counter").value),
    }),
  });
  $("#pool-status").textContent = response.ok ? "saved" : "save failed";
  setTimeout(() => { $("#pool-status").textContent = ""; }, 2000);
}

// ---------- blocks ----------

async function loadBlocks() {
  const data = await getJSON("/api/blocks");
  blockState.blocks = data.blocks;
  blockState.blockSize = data.block_size;
  renderBlocks();
  await renderBlockPicker();
}

function blockGameRow(g) {
  return `<tr>
    <td>${fmtDate(g.game_creation_ms)}</td>
    <td>${escapeHtml(g.account)}</td>
    <td><span class="champ-cell">${champIcon(g.my_champion)}${displayName(g.my_champion)}</span></td>
    <td><span class="champ-cell">${g.opp_champion ? champIcon(g.opp_champion) + "vs " + displayName(g.opp_champion) : "–"}</span></td>
    <td><span class="result-pill ${g.win ? "win" : "loss"}">${g.win ? "W" : "L"}</span></td>
    <td>${g.kills}/${g.deaths}/${g.assists}</td>
    <td class="notes-cell"><textarea class="game-notes" data-entry="${g.entry_id}"
        rows="1" placeholder="notes…">${escapeHtml(g.notes)}</textarea></td>
    <td><button class="preset game-remove" data-entry="${g.entry_id}" title="Remove from block">×</button></td>
  </tr>`;
}

function blockCard(block, isCurrent) {
  const wins = block.games.filter((g) => g.win).length;
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
    learnings = `<div class="session-body md-body">${block.learnings
      ? renderNotes(block.learnings)
      : `<p class="muted">No learnings recorded yet.</p>`}</div>`;
  }
  return `<div class="session-card block-card">
    <div class="session-head">
      <span class="session-date">Block #${block.id}</span>
      ${isCurrent ? `<span class="block-badge">current</span>` : ""}
      <span class="muted">${block.games.length}/${blockState.blockSize} games
        ${block.games.length ? `· ${wins}–${block.games.length - wins}` : ""}</span>
      <input type="text" class="block-title" data-id="${block.id}"
        value="${escapeHtml(block.title)}" placeholder="block title…">
      <span class="session-actions">
        <button class="preset learnings-edit" data-id="${block.id}">edit learnings</button>
        <button class="preset block-delete" data-id="${block.id}">delete</button>
      </span>
    </div>
    ${block.games.length ? `<div class="table-wrap block-games"><table>
      <thead><tr><th>Date</th><th>Account</th><th>Me</th><th>Opponent</th>
      <th>Result</th><th>K/D/A</th><th class="notes-col">Notes</th><th></th></tr></thead>
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
  const autoGrow = (el) => { el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; };
  target.querySelectorAll(".game-notes").forEach((input) => {
    autoGrow(input);
    input.addEventListener("input", () => autoGrow(input));
    input.addEventListener("keydown", (e) => {
      // Enter saves (blur); Shift+Enter inserts a new line
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        input.blur();
      }
    });
    input.addEventListener("change", () =>
      fetch(`/api/blocks/games/${input.dataset.entry}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: input.value }),
      }));
  });
  target.querySelectorAll(".game-remove").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this game from the block?")) return;
      await fetch(`/api/blocks/games/${btn.dataset.entry}`, { method: "DELETE" });
      loadBlocks();
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
