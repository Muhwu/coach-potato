# Block Learnings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Champion-pool commitment + auto-advancing three-game blocks with per-game notes and Markdown learnings; games added from existing data (Blocks-tab picker or promote buttons on match lists).

**Architecture:** Three new tables + helpers in `server/db.py`; hydration join in `server/stats.py`; REST endpoints in `server/app.py`; new `static/blocks.js` view + promote buttons in `app.js` tables.

**Tech Stack:** unchanged.

## Global Constraints

- BLOCK_SIZE = 3; current block = **newest** block; complete = ≥3 games (derived); adding to a full/absent current block creates the next one.
- `block_games UNIQUE(match_id, puuid)` — one block per game; POST duplicate → 409 including the holding block id.
- Pool roles exactly `main_blind` (single) / `core` / `counter`; PUT replaces wholesale.
- All game fields except `notes` auto-populate from stored matches.

---

### Task 1: DB — pool + blocks helpers

**Files:** Modify `server/db.py` (SCHEMA + helpers); test `tests/test_db.py`.

**Produces:** `get_pool(conn) -> {main_blind: str|None, core: list, counter: list}`; `set_pool(conn, main_blind, core, counter)`; `create_block(conn) -> int`; `add_game_to_block(conn, match_id, puuid) -> int` (block id; sqlite3.IntegrityError on dup); `find_block_for_game(conn, match_id, puuid) -> int|None`; `list_blocks(conn)` desc; `update_block(conn, block_id, title=None, learnings=None) -> bool`; `update_block_game(conn, entry_id, notes) -> bool`; `delete_block_game(conn, entry_id) -> bool`; `delete_block(conn, block_id) -> bool` (cascade); `BLOCK_SIZE = 3`.

- [ ] Failing tests: pool round-trip + wholesale replace + empty default; first add creates block 1; adds 2–3 stay in block 1; 4th add creates block 2; duplicate raises IntegrityError + find_block_for_game locates holder; update/delete helpers incl. cascade. Implement → PASS → commit.

### Task 2: stats.block_games_detailed

**Files:** Modify `server/stats.py`; test `tests/test_stats.py`.

**Produces:** `block_games_detailed(conn) -> list[dict]` with keys `entry_id, block_id, notes, match_id, puuid, game_creation_ms, game_duration_s, queue_id, my_champion, win, kills, deaths, assists, opp_champion` ordered by game_creation_ms asc.

- [ ] Failing tests: hydrates champion/opponent/result from add_match fixtures; game without enemy TOP → opp_champion None. Implement (JOIN participants me / matches / LEFT JOIN enemy-TOP participant) → PASS → commit.

### Task 3: API endpoints

**Files:** Modify `server/app.py`; test `tests/test_app.py`.

**Produces:** `GET/PUT /api/pool`; `GET /api/blocks` (blocks desc, each with `complete` and hydrated `games` + `account`); `POST /api/blocks/games` (201-ish {block_id}, 409 w/ block id in detail, 404 unknown pair); `PATCH /api/blocks/{id}`; `PATCH /api/blocks/games/{id}`; `DELETE /api/blocks/games/{id}`; `DELETE /api/blocks/{id}`. `/api/stats/games` rows keep `my_puuid`.

- [ ] Failing tests: pool GET default/PUT round trip + 400 on non-list core; add-game auto-advance visible via GET /api/blocks; 409 detail names holding block; 404 pair; PATCH/DELETE paths incl. 404s; games endpoint includes my_puuid. Implement → PASS → commit.

### Task 4: Blocks tab frontend

**Files:** Create `static/blocks.js`; modify `static/index.html` (nav button, blocks-view section, script tag), `static/app.js` (setMainView + hash `#blocks`, expose unionFilterOptions reuse), `static/style.css` (pool card, block cards, picker).

- [ ] Pool editor (main blind input + comma lists + datalist + Save), blocks list (current badge, title inline edit, learnings expand/edit markdown, games rows with notes inline save + remove, delete block), picker of last 10 unblocked games under current block. Suite green; screenshot verify.

### Task 5: Promote buttons + docs + push

**Files:** Modify `static/app.js` (Recent games + segment games "+" buttons wired to POST), `README.md`, `CLAUDE.md`.

- [ ] Buttons post {match_id, puuid} (recent uses state.puuid; segment rows use g.my_puuid); success flash "added to Block #N", 409 alert. Screenshot; docs; full pytest; commit; push.
