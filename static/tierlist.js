"use strict";
/* Champion tier-list maker. Uses globals from app.js/blocks.js: state, $,
   getJSON, escapeHtml, champIcon, displayName, roster, loadChampionRoster. */

const tierState = {
  wired: false,
  lists: [],        // [{id, title, data, updated_at_ms}]
  activeId: null,
  tiers: [],        // working copy: [{label, color, champions:[id,...]}]
  search: "",
  saveTimer: null,
};

const TIER_COLORS = ["#ff6b6b", "#ffa94d", "#ffd43b", "#94d82d", "#4dabf7", "#b197fc"];
function defaultTiers() {
  return ["S", "A", "B", "C", "D"].map((label, i) =>
    ({ label, color: TIER_COLORS[i] || "#868e96", champions: [] }));
}

async function initTiers() {
  if (!tierState.wired) {
    tierState.wired = true;
    $("#tier-new").addEventListener("click", createTierList);
    $("#tier-new-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); createTierList(); }
    });
    $("#tier-rename").addEventListener("click", renameTierList);
    $("#tier-delete").addEventListener("click", deleteTierList);
    $("#tier-add-row").addEventListener("click", () => {
      tierState.tiers.push({ label: "", color: TIER_COLORS[tierState.tiers.length % TIER_COLORS.length], champions: [] });
      renderTiers(); saveTiers();
    });
    $("#tier-list-select").addEventListener("change", (e) => selectList(+e.target.value));
    $("#tier-search").addEventListener("input", (e) => {
      tierState.search = e.target.value.trim().toLowerCase();
      renderPool();
    });
    await loadChampionRoster(); // full roster + icons
  }
  await loadTierLists();
}

async function loadTierLists() {
  tierState.lists = await getJSON("/api/tier-lists").catch(() => []);
  if (!tierState.lists.length) {
    // start everyone off with one ready-to-use list
    const created = await postTierList({ title: "My tier list", data: { tiers: defaultTiers() } });
    if (created) tierState.lists = [created];
  }
  if (!tierState.lists.some((l) => l.id === tierState.activeId)) {
    tierState.activeId = tierState.lists.length ? tierState.lists[0].id : null;
  }
  loadActiveIntoState();
  renderListSelect();
  renderTiers();
}

function loadActiveIntoState() {
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  const tiers = (list && list.data && list.data.tiers) || [];
  tierState.tiers = tiers.map((t) => ({
    label: t.label || "", color: t.color || "#868e96", champions: [...(t.champions || [])],
  }));
  if (!tierState.tiers.length) tierState.tiers = defaultTiers();
}

function renderListSelect() {
  $("#tier-list-select").innerHTML = tierState.lists
    .map((l) => `<option value="${l.id}"${l.id === tierState.activeId ? " selected" : ""}>${escapeHtml(l.title)}</option>`)
    .join("");
  const one = tierState.lists.length <= 1;
  $("#tier-delete").disabled = one; // keep at least one list around
}

function selectList(id) {
  tierState.activeId = id;
  loadActiveIntoState();
  renderTiers();
}

// ---------- rendering ----------

const placedIds = () => new Set(tierState.tiers.flatMap((t) => t.champions));

function champChip(id) {
  return `<span class="tier-chip" draggable="true" data-champ="${escapeHtml(id)}"
    title="${escapeHtml(displayName(id))}">${champIcon(id)}</span>`;
}

function renderTiers() {
  $("#tier-rows").innerHTML = tierState.tiers.map((t, i) => `
    <div class="tier-row" data-tier="${i}">
      <div class="tier-label" style="background:${escapeHtml(t.color)}">
        <input class="tier-label-input" data-tier="${i}" value="${escapeHtml(t.label)}"
          maxlength="24" aria-label="Tier label" spellcheck="false">
        <div class="tier-label-actions">
          <input type="color" class="tier-color" data-tier="${i}" value="${escapeHtml(t.color)}"
            title="Tier colour" aria-label="Tier colour">
          <button class="tier-del icon-btn-sm" data-tier="${i}" title="Remove tier"
            aria-label="Remove tier">🗑</button>
        </div>
      </div>
      <div class="tier-drop" data-tier="${i}">${t.champions.map(champChip).join("")}</div>
    </div>`).join("");
  wireTierRows();
  renderPool();
}

function renderPool() {
  const placed = placedIds();
  const all = [...roster.nameById.keys()]
    .filter((id) => !placed.has(id))
    .sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const shown = tierState.search
    ? all.filter((id) => displayName(id).toLowerCase().includes(tierState.search))
    : all;
  $("#tier-pool").innerHTML = shown.map(champChip).join("")
    || `<span class="muted">${tierState.search ? "No champions match." : "Every champion is ranked."}</span>`;
  $("#tier-pool-count").textContent = `(${shown.length})`;
  wireChips($("#tier-pool"));
}

// ---------- drag & drop ----------

function moveChamp(id, targetTier /* index or null for pool */) {
  for (const t of tierState.tiers) {
    const k = t.champions.indexOf(id);
    if (k !== -1) t.champions.splice(k, 1);
  }
  if (targetTier != null && tierState.tiers[targetTier]) {
    tierState.tiers[targetTier].champions.push(id);
  }
  renderTiers();
  saveTiers();
}

function wireChips(container) {
  container.querySelectorAll(".tier-chip").forEach((chip) => {
    chip.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/champ", chip.dataset.champ);
      e.dataTransfer.effectAllowed = "move";
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => chip.classList.remove("dragging"));
  });
}

function wireDropZone(el, targetTier) {
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drag-over"); });
  el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drag-over");
    const id = e.dataTransfer.getData("text/champ");
    if (id) moveChamp(id, targetTier);
  });
}

function wireTierRows() {
  const box = $("#tier-rows");
  box.querySelectorAll(".tier-drop").forEach((z) => {
    wireChips(z);
    wireDropZone(z, +z.dataset.tier);
  });
  box.querySelectorAll(".tier-label-input").forEach((inp) =>
    inp.addEventListener("change", () => {
      tierState.tiers[+inp.dataset.tier].label = inp.value.slice(0, 24);
      saveTiers();
    }));
  box.querySelectorAll(".tier-color").forEach((inp) =>
    inp.addEventListener("change", () => {
      const i = +inp.dataset.tier;
      tierState.tiers[i].color = inp.value;
      inp.closest(".tier-row").querySelector(".tier-label").style.background = inp.value;
      saveTiers();
    }));
  box.querySelectorAll(".tier-del").forEach((btn) =>
    btn.addEventListener("click", () => {
      tierState.tiers.splice(+btn.dataset.tier, 1); // its champions fall back to the pool
      renderTiers(); saveTiers();
    }));
  // dropping onto the pool panel un-ranks a champion
  wireDropZone($("#tier-pool"), null);
}

// ---------- persistence ----------

function tierStatus(msg) { $("#tier-status").textContent = msg; }

async function postTierList(body) {
  const res = await fetch("/api/tier-lists", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.ok ? res.json() : null;
}

function saveTiers() {
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (list) list.data = { tiers: tierState.tiers };  // keep local copy in sync
  clearTimeout(tierState.saveTimer);
  tierStatus("saving…");
  tierState.saveTimer = setTimeout(async () => {
    const res = await fetch(`/api/tier-lists/${tierState.activeId}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: { tiers: tierState.tiers } }),
    }).catch(() => null);
    tierStatus(res && res.ok ? "saved ✓" : "save failed");
    setTimeout(() => tierStatus(""), 1500);
  }, 350);
}

async function createTierList() {
  const nameInput = $("#tier-new-name");
  const title = nameInput.value.trim() || "New tier list";
  const created = await postTierList({ title, data: { tiers: defaultTiers() } });
  if (!created) { tierStatus("create failed"); return; }
  nameInput.value = "";
  tierState.lists.push(created);
  tierState.activeId = created.id;
  loadActiveIntoState();
  renderListSelect();
  renderTiers();
}

async function renameTierList() {
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (!list) return;
  const title = (prompt("Rename tier list:", list.title) || "").trim();
  if (!title || title === list.title) return;
  const res = await fetch(`/api/tier-lists/${list.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  }).catch(() => null);
  if (res && res.ok) { list.title = title; renderListSelect(); }
}

async function deleteTierList() {
  if (tierState.lists.length <= 1) return;
  const list = tierState.lists.find((l) => l.id === tierState.activeId);
  if (!list || !confirm(`Delete tier list "${list.title}"?`)) return;
  const res = await fetch(`/api/tier-lists/${list.id}`, { method: "DELETE" }).catch(() => null);
  if (!(res && res.ok)) { tierStatus("delete failed"); return; }
  tierState.lists = tierState.lists.filter((l) => l.id !== list.id);
  tierState.activeId = tierState.lists[0].id;
  loadActiveIntoState();
  renderListSelect();
  renderTiers();
}
