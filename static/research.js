"use strict";
/* Research view: a study journal for other players' games — not tied to
   the tracked-account crawler at all, just freeform entries: who you're
   studying, optionally which champion/matchup, one Markdown notes field
   (covers general observations and VOD notes together — no separate
   timestamp log), and screenshots (multiple, no caption needed).
   Deliberately no video/clip attachments here — screenshots only.
   Collapsed by default; detail (notes/screenshots) is lazy-loaded on first
   expand, matching the rest of the app.
   Uses globals from app.js: $, getJSON, escapeHtml, displayName, champIcon,
   renderNotes, setMainView.
   Uses roster/loadChampionRoster from blocks.js for the shared #champ-list
   datalist (champion/opponent fields are optional freeform text here, not
   validated against played matchups). */

const researchState = {
  wired: false,
  entries: [],
  expanded: new Set(),
  detail: new Map(),      // entry id -> full entry (notes, screenshots)
  editingNotes: null,     // entry id whose notes textarea is open
  shotFormOpen: new Set(), // entry ids whose "add screenshot" form is open
};

async function initResearch() {
  if (!researchState.wired) {
    researchState.wired = true;
    $("#research-add-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("#research-add-status");
      const player = $("#research-player").value.trim();
      if (!player) { status.textContent = "player name is required"; return; }
      const response = await fetch("/api/research", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          player_name: player,
          champion: $("#research-champion").value.trim(),
          opp_champion: $("#research-opp-champion").value.trim(),
          title: $("#research-title").value.trim(),
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      status.textContent = "";
      $("#research-player").value = "";
      $("#research-champion").value = "";
      $("#research-opp-champion").value = "";
      $("#research-title").value = "";
      await loadResearch();
    });
  }
  await loadChampionRoster(); // populates the shared #champ-list datalist
  await loadResearch();
}

async function loadResearch() {
  researchState.entries = await getJSON("/api/research");
  renderResearch();
}

async function ensureResearchDetail(id) {
  if (!researchState.detail.has(id)) {
    researchState.detail.set(id, await getJSON(`/api/research/${id}`));
  }
}

function researchEntryHeader(entry, collapsed) {
  const champVs = entry.champion
    ? `<span class="champ-cell">${champIcon(entry.champion)}${displayName(entry.champion)}${
        entry.opp_champion
          ? ` vs ${champIcon(entry.opp_champion)}${displayName(entry.opp_champion)}` : ""}</span>`
    : "";
  return `<div class="session-head research-head">
    <button class="preset icon-btn session-toggle research-toggle" data-id="${entry.id}"
      aria-expanded="${!collapsed}" title="${collapsed ? "Expand" : "Collapse"}">${collapsed ? "▸" : "▾"}</button>
    <span class="research-player">${escapeHtml(entry.player_name)}</span>
    ${champVs}
    ${entry.title ? `<span class="muted">${escapeHtml(entry.title)}</span>` : ""}
    <span class="session-actions">
      <button class="preset icon-btn research-delete" data-id="${entry.id}"
        title="Delete entry" aria-label="Delete entry">🗑</button>
    </span>
  </div>`;
}

function researchNotesBlock(entry, detail) {
  if (researchState.editingNotes === entry.id) {
    return `<label class="filter-label" for="research-notes-${entry.id}">Notes (Markdown)</label>
      <textarea id="research-notes-${entry.id}" rows="5"
        placeholder="General observations, VOD notes, timestamps, etc…">${escapeHtml(detail.notes)}</textarea>
      <div class="session-actions">
        <button class="preset research-notes-save" data-id="${entry.id}">Save</button>
        <button class="preset research-notes-cancel">Cancel</button>
        <span class="muted research-notes-status"></span>
      </div>`;
  }
  return `<div class="md-body">${detail.notes ? renderNotes(detail.notes) : `<p class="muted">No notes yet.</p>`}</div>`;
}

function researchEntryBody(entry) {
  const detail = researchState.detail.get(entry.id);
  if (!detail) return `<div class="session-body"><p class="muted">Loading…</p></div>`;
  const editing = researchState.editingNotes === entry.id;
  const screenshots = detail.screenshots.length
    ? detail.screenshots.map((s) => `<div class="research-screenshot">
        <img src="${s.file_url}" alt="" loading="lazy">
        <button class="preset chip-x research-shot-remove" data-id="${s.id}"
          title="Remove screenshot" aria-label="Remove screenshot">×</button>
      </div>`).join("")
    : `<p class="muted">No screenshots yet.</p>`;
  const shotForm = researchState.shotFormOpen.has(entry.id)
    ? `<form class="research-shot-form" data-id="${entry.id}">
        <div class="research-shot-row">
          <input type="file" class="research-shot-file-input" accept="image/png,image/jpeg,image/webp,image/gif">
          <button type="submit" class="preset">Attach</button>
          <button type="button" class="preset research-shot-cancel">Cancel</button>
          <span class="muted research-shot-status"></span>
        </div>
      </form>`
    : `<button type="button" class="preset research-shot-open" data-id="${entry.id}">+ Add screenshot</button>`;
  return `<div class="session-body">
    <div class="research-sub-head">
      <h4>Notes</h4>
      ${editing ? "" : `<button class="preset icon-btn research-notes-edit" data-id="${entry.id}"
        title="Edit notes" aria-label="Edit notes">✎</button>`}
    </div>
    ${researchNotesBlock(entry, detail)}
    <div class="research-sub-head"><h4>Screenshots</h4></div>
    <div class="research-screenshots">${screenshots}</div>
    ${shotForm}
  </div>`;
}

function researchEntryCard(entry) {
  const collapsed = !researchState.expanded.has(entry.id);
  return `<div class="session-card" data-id="${entry.id}">
    ${researchEntryHeader(entry, collapsed)}
    ${collapsed ? "" : researchEntryBody(entry)}
  </div>`;
}

function renderResearch() {
  const target = $("#research-list");
  target.innerHTML = researchState.entries.length
    ? researchState.entries.map(researchEntryCard).join("")
    : `<div class="empty">No research entries yet — add one below.</div>`;
  wireResearchHandlers(target);
}

function wireResearchHandlers(target) {
  target.querySelectorAll(".research-toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.id;
      if (researchState.expanded.has(id)) {
        researchState.expanded.delete(id);
      } else {
        researchState.expanded.add(id);
        await ensureResearchDetail(id);
      }
      renderResearch();
    }));
  target.querySelectorAll(".research-delete").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.id;
      if (!confirm("Delete this research entry? This can't be undone.")) return;
      await fetch(`/api/research/${id}`, { method: "DELETE" });
      researchState.expanded.delete(id);
      researchState.detail.delete(id);
      await loadResearch();
    }));
  target.querySelectorAll(".research-notes-edit").forEach((btn) =>
    btn.addEventListener("click", () => {
      researchState.editingNotes = +btn.dataset.id;
      renderResearch();
    }));
  target.querySelectorAll(".research-notes-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      researchState.editingNotes = null;
      renderResearch();
    }));
  target.querySelectorAll(".research-notes-save").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.id;
      const notes = $(`#research-notes-${id}`).value;
      const status = btn.parentElement.querySelector(".research-notes-status");
      const response = await fetch(`/api/research/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      researchState.detail.set(id, await response.json());
      researchState.editingNotes = null;
      renderResearch();
    }));
  target.querySelectorAll(".research-shot-open").forEach((btn) =>
    btn.addEventListener("click", () => {
      researchState.shotFormOpen.add(+btn.dataset.id);
      renderResearch();
    }));
  target.querySelectorAll(".research-shot-cancel").forEach((btn) =>
    btn.addEventListener("click", () => {
      researchState.shotFormOpen.delete(+btn.closest(".session-card").dataset.id);
      renderResearch();
    }));
  target.querySelectorAll(".research-shot-form").forEach((form) =>
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = +form.dataset.id;
      const fileInput = form.querySelector(".research-shot-file-input");
      const status = form.querySelector(".research-shot-status");
      const file = fileInput.files[0];
      if (!file) { status.textContent = "choose a file first"; return; }
      status.textContent = "uploading…";
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(`/api/research/${id}/screenshots`, { method: "POST", body: formData });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || `error ${response.status}`;
        return;
      }
      researchState.detail.get(id).screenshots = await response.json();
      renderResearch();
    }));
  target.querySelectorAll(".research-shot-remove").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const shotId = +btn.dataset.id;
      const entryId = +btn.closest(".session-card").dataset.id;
      await fetch(`/api/research/screenshots/${shotId}`, { method: "DELETE" });
      const detail = researchState.detail.get(entryId);
      detail.screenshots = detail.screenshots.filter((s) => s.id !== shotId);
      renderResearch();
    }));
}
