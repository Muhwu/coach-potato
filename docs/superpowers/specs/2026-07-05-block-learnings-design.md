# Block Learnings — Design

**Date:** 2026-07-05
**Status:** Approved by user (auto-advance blocks, Markdown block learnings,
one standing pool, new Blocks tab).

## Purpose

Track deliberate-practice "blocks": a standing champion-pool commitment
(Main Blind / Core Pool / Counter Picks) and three-game blocks whose games
carry champion, matchup, result (auto-populated from stored matches) and
hand-written notes, plus a Markdown "learnings" summary per block.

## Data model

- `champion_pool(role TEXT CHECK(role IN ('main_blind','core','counter')),
  champion TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0)` — replaced
  wholesale on save. Main blind: single entry.
- `blocks(id INTEGER PK AUTOINCREMENT, title TEXT NOT NULL DEFAULT '',
  learnings TEXT NOT NULL DEFAULT '', created_at_ms INTEGER)`.
- `block_games(id INTEGER PK AUTOINCREMENT, block_id INTEGER NOT NULL,
  match_id TEXT NOT NULL, puuid TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '', added_at_ms INTEGER,
  UNIQUE(match_id, puuid))` — a game belongs to at most one block.

Rules: BLOCK_SIZE = 3. **Current block = newest block.** A block is
*complete* when it has ≥3 games (derived, no status column). Adding a game
when there is no block, or the newest is full, creates the next block.
Removing games never changes which block is current.

## DB helpers (`server/db.py`)

`get_pool(conn) -> {main_blind: str|None, core: [str], counter: [str]}`,
`set_pool(conn, main_blind, core, counter)`, `create_block(conn) -> id`,
`add_game_to_block(conn, match_id, puuid) -> block_id` (auto-advance;
sqlite3.IntegrityError on duplicate), `list_blocks(conn)` (newest first),
`update_block(conn, id, title=None, learnings=None) -> bool`,
`update_block_game(conn, id, notes) -> bool`,
`delete_block_game(conn, id) -> bool`, `delete_block(conn, id) -> bool`
(cascades its games).

## Hydration (`server/stats.py`)

`block_games_detailed(conn) -> list[dict]`: block_games joined to
participants/matches + enemy-TOP participant → entry_id, block_id, notes,
match_id, puuid, game_creation_ms, game_duration_s, queue_id, my_champion,
win, kills, deaths, assists, opp_champion. Ordered by game_creation_ms.

## API (`server/app.py`)

- `GET /api/pool` / `PUT /api/pool` (`{main_blind, core, counter}`;
  400 on non-list core/counter).
- `GET /api/blocks` → `{blocks: [{id, title, learnings, created_at_ms,
  complete, games: [... + account]}]}` newest first.
- `POST /api/blocks/games {match_id, puuid}` → `{block_id}`;
  409 (+ existing block id in detail) if already in a block; 404 when the
  (match, participant) pair isn't in the db.
- `PATCH /api/blocks/{id} {title?, learnings?}` (400 both absent, 404).
- `PATCH /api/blocks/games/{id} {notes}` (404). `DELETE` both (404;
  block delete cascades, confirm() in UI).

## Frontend

New **Blocks** tab (`#blocks` hash, `static/blocks.js`):
- **Champion pool card**: Main Blind (single input), Core Pool and Counter
  Picks (comma-separated inputs), champion-name datalist from union filter
  options, Save button.
- **Blocks list**, newest first; current (newest) block badged "current".
  Per block: header `Block #N · n/3 · W–L`, editable title, Markdown
  learnings (same expand/edit pattern as session notes), games rows
  (champ icon, vs opponent, W/L pill, K/D/A, date, account, inline notes
  input saved on change/blur, remove ×), delete block.
- **Game picker** under the current block: last 10 top-lane games not in
  any block with an Add button each.
- **Promote buttons**: a "+" button on Overview Recent-games rows and on
  coaching segment nested-games rows → POST; success shows "added to
  Block #N", 409 alerts which block already holds it.
  (`/api/stats/games` keeps `my_puuid` in rows so the button knows the
  participant; overview recent uses the selected account's puuid.)

## Testing

TDD: pool round-trip/replace; auto-advance across the 3-game boundary;
duplicate rejection; hydration join incl. missing-opponent game; API shapes
+ error codes. Screenshot verification of the Blocks tab and promote flow.

## Out of scope (YAGNI)

Pool history/snapshots, variable block sizes, per-block winrate analytics,
manual entry of games not present in the db, reordering games.
