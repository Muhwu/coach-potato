# CLAUDE.md

**Coach Potato** — coaching & improvement app for LoL: crawls Riot match
history for the configured accounts into sqlite, serves matchup stats /
coaching progress / trends / block learnings via FastAPI + vanilla-JS
frontend. Stats are currently top-lane scoped (`team_position='TOP'` in
`stats._BASE`); all roles' data is stored, so generalizing is a query/UI
change, not a crawler change.

## Commands

```bash
./setup.sh                                  # venv + deps + .env
.venv/bin/python -m pytest tests/ -q        # tests (offline, fast — run before committing)
./crawl.sh --limit 5                        # SMALL live batch — always test crawler changes this way first
./crawl.sh                                  # full incremental crawl
./run.sh                                    # uvicorn on http://localhost:8321
```

## Gotchas that matter here

- **Runtime settings live in the db `settings` table** (Settings view /
  `/api/settings`), with `.env` as read-through fallback for dev
  (`config.resolve_settings`; tests monkeypatch `config.ENV_FALLBACK_ROOT`).
  The web app no longer needs `.env`; `crawl.py` CLI still uses it.
  `config.default_db_path()`: LOL_DB_PATH → env; frozen → OS app-data dir;
  else `data/lol.sqlite`. `desktop.py` + PyInstaller (`--add-data
  static:static`) produce the packaged build; CI matrix in
  `.github/workflows/build.yml`. `desktop.py` redirects `sys.stdout`/
  `stderr` to `os.devnull` when they're `None` (before importing uvicorn) —
  a `--windowed` build has no console on Windows, so those streams are
  `None`, and uvicorn's logging setup crashes calling `.isatty()` on `None`
  before the app window ever opens. Any future PyInstaller/`--windowed`
  build issue that looks like a startup crash touching logging/stdout is
  probably this same class of bug. CI also builds a Windows installer
  (`packaging/windows-installer.iss`, Inno Setup 6 — preinstalled on the
  `windows-latest` runner image, no setup step needed) that wraps the same
  PyInstaller exe; per-user install under `%LOCALAPPDATA%` (no admin/UAC),
  matching where the app's own data already lives (`%APPDATA%\CoachPotato`).
  Uploaded as its own `coach-potato-windows-installer` artifact alongside
  the plain portable `.exe`, not replacing it.

- **Dev API key expires every 24 h.** 403 → `ApiKeyExpiredError`. Refresh at
  developer.riotgames.com, update `RIOT_API_KEY=` in `.env` (gitignored).
- **`cryptography` is a real (compiled) dependency**, added for champ-guide
  export/import encryption (`server/crypto.py`). Confirmed building clean
  through CI `build.yml` on all three OSes. `python-multipart` was added
  for the clips feature's file-upload endpoints (`Form`/`File`/`UploadFile`
  in app.py) — FastAPI raises at startup if it's missing, easy to mis-diagnose.
- **Rate limits: 20 req/1 s and 100 req/2 min**, enforced by
  `RateLimiter` in `server/riot_client.py`. Never bypass it; test crawler
  changes with `--limit 5` before any full crawl.
- **Riot ID quirks:** league-v4 uses the platform host (e.g. `euw1`), match-v5
  the regional host (e.g. `europe`), account-v1 only exists on
  americas/asia/europe (sea platforms fall back to asia). All derived from the
  `PLATFORM` env var via `PLATFORM_ROUTING` in `server/riot_client.py`.
  Unicode Riot IDs must be URL-encoded (the client does it).
- Champion names from match-v5 are DDragon keys (`MonkeyKing` = Wukong);
  the frontend maps display names in `DISPLAY_NAME_FIXES`.
- `static/champions.json` is the static champion roster (ids + display
  names) used for pool autocomplete/validation (client + `CHAMPION_IDS` in
  app.py). Refresh after new champion releases:
  fetch DDragon versions.json → cdn/<ver>/data/en_US/champion.json →
  regenerate the file (see git history of the file for the exact script).
- `static/runes.json` is the static rune tree/row/shard roster (names, icon
  paths, and numeric match-v5 ids) that drives the Champ guide rune-page
  picker (client + `server/rune_data.py`, the single loader both `app.py`
  validation and `crawler.py` decoding go through). Refresh after a rune
  rework: DDragon versions.json → cdn/<ver>/data/en_US/runesReforged.json
  for trees/keystones/minors (`id`/`name`/`icon`, icon paths as-is, served
  from `ddragon.leagueoflegends.com/cdn/img/<icon>`); stat shards aren't in
  that file — pull them from CommunityDragon's
  `rcp-be-lol-game-data/global/default/v1/perks.json` for name+id, icons
  served from `raw.communitydragon.org/.../perk-images/statmods/<icon
  lowercased>`. **The numeric `id` fields must stay correct** — they're how
  `rune_data.decode_perks()` turns a match-v5 participant's `perks` payload
  (tree/rune/shard ids) into the same rune-page shape as the champ guide.
- Timestamps are **ms epoch** everywhere in the db; match-v5 `startTime`
  param is **seconds**.

## Architecture (one line each)

- `server/config.py` — `.env` parser; `load_config()` → key, db path, accounts.
- `server/riot_client.py` — httpx client + sliding-window limiter; 429 retry,
  5xx backoff; injectable `transport`/`clock` for tests.
- `server/parsing.py` — match-v5 JSON → `(match_row, participant_rows)`.
- `server/crawler.py` — `Crawler.crawl_player()` pages match ids with a
  watermark in `crawl_state` (incomplete crawls re-page full history; detail
  fetches are skipped for stored matches, so it's cheap). `enrich_ranks()`
  fetches lane opponents' current solo rank (7-day TTL in `player_ranks`).
  `_store_metrics()`/`_store_runes()` run inline per new match for tracked
  participants (coaching metrics, actual runes played); `backfill_metrics()`/
  `backfill_runes()` re-fetch stored matches missing either.
- `server/stats.py` — all aggregation in SQL over a filtered base query;
  matchup = tracked TOP player joined to enemy TOP participant; remakes
  (<300 s) excluded; opponent rank bucket `UNKNOWN` when not fetched.
- `server/app.py` — FastAPI; per-request sqlite connections; crawl runs in a
  daemon thread with module-level `CRAWL_STATE`; db path override via
  `LOL_DB_PATH` env (used by tests). "Hide my rank / LP" setting
  (`hide_my_rank`) redacts own-rank fields (`_MY_RANK_KEYS`) from ALL JSON
  API responses via middleware — new endpoints get hiding for free as long as
  they reuse those key names (`solo_*`, `start_ranks`, `end_ranks`); anything
  else rank-shaped needs its own check (see the rank-history endpoint).
  Appearance settings: `ui_opacity` (20-100, default 100) and an optional
  uploaded background picture (`POST/DELETE /api/settings/background`,
  served via `GET /api/settings/background/file`; stored as a single file
  in `<db_dir>/background/`, filename tracked by the `background_image_file`
  settings key, replaced/deleted on re-upload). CSS applies both app-wide via
  `--ui-opacity` (set on `:root` from JS) driving `--surface-1`/`--page`
  through `color-mix(...)` — every existing `var(--surface-1)`/`var(--page)`
  usage becomes translucent for free, no per-component changes — plus a
  fixed, full-viewport `#bg-image` div (z-index -1) behind everything for the
  picture itself. `accent_color` (optional `#rrggbb` hex, `HEX_COLOR_RE`
  validated; `None`/unset = theme default) overrides `--series-1` the same
  way — set inline on `:root` from JS, removed to fall back to the
  stylesheet's light/dark default. `--accent-wash` derives from `--series-1`
  via `color-mix(...)` too, so every accent-tinted background/border (active
  tabs, buttons, chip highlights) follows a custom color for free — watch
  for new one-off `rgba(42, 120, 214, ...)`-style literals bypassing this.
  Session CRUD at `/api/sessions`;
  `/api/stats/progress` aggregates across ALL tracked puuids (no puuid param).
- Sessions have `title` + Markdown `notes` (legacy `note` column auto-migrates
  in `db._migrate`). `PATCH /api/sessions/{id}` edits them;
  `GET /api/sessions/export.md` produces the all-sessions Markdown export.
  Markdown renders client-side via vendored `static/vendor/marked.min.js`
  (no CDN at runtime; update by re-downloading from jsdelivr). A session
  card's Clips section (see `clips` table below) only loads when the card
  is expanded.
- Segment rows expand to per-game lists: `stats.games_in_range(conn, puuids,
  from_ms, to_ms, ...)` behind `GET /api/stats/games?from_ms=&to_ms=` (ms
  bounds; client passes `to_ms-1` for half-open segments); frontend caches
  per segment in `segmentUi.cache`, cleared on every loadProgress.
- Coaching progress: `coaching_sessions` table (global, unique ISO date);
  `stats.progress_segments(conn, puuids, sessions, ...)` returns
  baseline + between + since-last segments, half-open at session-date UTC
  midnight. `_filtered_base` accepts a puuid list for multi-account queries.
  Frontend defaults the progress champion filter to Gwen; `#progress` hash
  deep-links the view.
- Coaching metrics: `server/metrics.py` is the single-source registry
  (labels/groups/agg kinds/directions) driving the `participant_metrics`
  DDL, payload parsing, SQL aggregation and frontend meta. Stored for
  tracked players only; crawler captures on insert;
  `crawler.backfill_metrics()` / `./crawl.sh --backfill-metrics` re-fetches
  older matches. `stats.segment_metrics` (per period) and
  `stats.trend_buckets` (day/week/month; week = Monday date) feed
  `/api/stats/metrics` and `/api/stats/trends` (both include `meta`).
- Block learnings: `champion_pool` (role main_blind/core/counter, replaced
  wholesale), `blocks` + `block_games` (UNIQUE match_id+puuid). Current block
  = newest; block size is a setting (`db.get_block_size`, >=1, no upper
  bound, default `db.BLOCK_SIZE`=3); complete = closed early, pool-snapshot stamped
  (finalized under an earlier size), or ≥size games;
  `db.add_game_to_block` auto-advances. Time-gap auto-close: adding a game
  whose game time is > `block_gap_hours` (setting, default 3 h, 0 = off)
  from the open block's latest game 412s with `{"reason": "gap"}` for
  client confirmation (skipped when `block_gap_confirm` is off), then
  closes the block (`db.block_gap_exceeded`). Hydration via
  `stats.block_games_detailed`. API: `/api/pool`, `/api/blocks`,
  `POST /api/blocks/games` (409 names holding block),
  `GET /api/blocks/game-notes?opp_champion=` (read-only; feeds the matchup
  Block-notes section, `focusBlock(id)` in `blocks.js` deep-links a block
  card). UI in `blocks.js`; "+ Block" promote buttons on Recent-games and
  segment game rows. A block game's Clips section (see `clips` table below)
  lives in its per-game stats panel, loaded together on first expand.
- `static/` — no build step; state + fetch + innerHTML render in `app.js`;
  matchups view (own tab: expanded rows with Overview [win/loss strip + block
  notes] / Games tabs; a 📖 link per row — shown only when a specific "My
  champion" filter is active, since guides are scoped per champion pair —
  deep-links to that matchup's Champ guide) in `matchups.js`;
  trends view (SVG small-multiple charts + breakdown table) in `trends.js`;
  blocks view in `blocks.js`; Champ guide view (own nav tab: pick "My
  champion" from the full roster — not just played champions — see/edit
  general champion notes, full rune pages + patch + notes for every matchup
  it has faced, or add one for a matchup not yet played via the shared
  champion-roster autocomplete from `blocks.js`; each matchup's "Recent
  games" column shows real games with the actual runes played, when
  recorded; Export/Import menus export/import one champion's whole guide as
  JSON, optionally password-encrypted) in `guide.js`.

## Schema (data/lol.sqlite)

`players(puuid PK, game_name, tag_line, is_tracked, solo_tier/division/lp, rank_fetched_at_ms)`
`matches(match_id PK, queue_id, game_creation_ms, game_duration_s, game_version, crawled_at_ms)`
`participants(match_id+puuid PK, champion_name, team_id, team_position, win, k/d/a, cs, gold_earned, damage_to_champions, riot_id_name)`
`player_ranks(puuid PK, solo_tier/division/lp, fetched_at_ms)` — opponent rank cache
`rank_history(puuid+fetched_at_ms PK, solo_tier/division/lp)` — tracked players'
rank snapshots: appended by `refresh_tracked_ranks()` each crawl, seeded once
from session/block `start_ranks`/`end_ranks` (`db.seed_rank_history`, runs on
connect while empty). Feeds the Overview "Rank over time" chart via
`/api/stats/rank-history` (`stats.rank_value` maps tier/division/LP to absolute
ladder points; coaching sessions drawn as vertical lines client-side).
Between/before snapshots, `stats._with_estimates` interleaves ±20 LP estimated
points from ranked-solo win/loss (`estimated: true`, rendered faint; each real
snapshot resets the drift, backward walk reconstructs pre-snapshot history).
`matchup_notes(my_champion+opp_champion PK, notes, runes, patch_version,
updated_at_ms)` — "Champ guide" scoped per (your champion, opponent
champion) pair: Markdown notes on how to play the matchup, a freeform patch
string, and `runes` — a JSON array of full rune pages (a matchup can carry
more than one, e.g. alternatives being tested). Each page: `{label,
primary_tree, keystone, primary_runes: [row1, row2, row3], secondary_tree,
secondary_runes: [rune, rune], shards: [offense, flex, defense]}`. Tree/rune/
shard data lives in `static/runes.json` (fetched from DDragon's
runesReforged.json + CommunityDragon's stat-shard perks; icons hotlinked at
request time — trees/runes via `ddragon.leagueoflegends.com/cdn/img/<icon>`,
shards via `raw.communitydragon.org/.../perk-images/statmods/<icon>`),
mirrored server-side as `RUNE_TREE_NAMES`/`RUNE_NAMES`/`RUNE_SHARD_NAMES` in
app.py for loose membership validation (no positional/row-consistency
checks — the picker UI is what enforces valid combinations).
`GET /api/matchups/notes?my_champion=` returns `{opp_champion: {notes,
runes, patch_version}}` for that champion (loose — "My champion" is chosen
from the full roster, not just played champions); `PUT /api/matchups/notes/
{my_champion}/{opp_champion}` is a full-row upsert (all fields blank deletes
the row). Own view: `guide.js` (pick "My champion" from the full roster,
see/edit every matchup it has faced or add one for a matchup not yet
played; each rune page is built with a full click-through picker —
primary tree → keystone + 3 minor rows, secondary tree → 2 minors from
different rows, 3 stat shards); the Matchups table's 📖 link deep-links
here via `openGuide()`. PK changed from opp_champion-only, and the old
single primary_keystone/secondary_tree columns collapsed into the `runes`
list, across two migrations in `db._migrate` (SQLite can't ALTER a primary
key, so both rebuild the table) — old rows land at `my_champion=''` since
neither predecessor schema tracked which champion notes were written for.
`champion_notes(champion PK, notes, updated_at_ms)` — general (non-matchup)
Markdown notes for a champion (build order, itemization…), shown above the
matchup list on the Champ guide page. `GET`/`PUT /api/champions/notes/
{champion}`.
Champ guide export/import (`server/crypto.py`): `POST /api/matchups/notes/
export` bundles one champion's `champion_notes` + all its `matchup_notes`
rows into a downloadable JSON file; an optional `password` in the request
body encrypts the payload (PBKDF2-HMAC-SHA256 key derivation + Fernet/
AES-128 via the `cryptography` package — a real cipher, not obfuscation).
`POST /api/matchups/notes/import/preview` decrypts (if needed) and reports
which opponents would be added/overwritten without writing anything;
`POST /api/matchups/notes/import` performs the writes. Wrong/missing
password on an encrypted file → 401. UI in `guide.js` (Export/Import menus
on the Champ guide page); import always shows the preview's overwrite
count in a `confirm()` before committing.
`crawl_state(puuid+queue_id PK, newest_ms, complete)` — resume watermarks
`participant_metrics(match_id+puuid PK, has_challenges, one REAL col per
metric key)` — coaching metrics, tracked players only, columns generated
from `server/metrics.py`
`participant_runes(match_id+puuid PK, runes)` — the rune page actually
played, decoded from match-v5's `perks` payload
(`server/rune_data.decode_perks`) into the same shape as a champ-guide rune
page; `runes` is `''` when a match legitimately had no perks data (so
`Crawler.backfill_runes()` doesn't keep re-fetching it). Stores rows for
every tracked participant **and their lane opponent** (same
`team_position`, other team — `Crawler._store_runes`, alongside
`_store_metrics`; the backfill query mirrors `enrich_ranks`' lane-opponent
join). Backfill via `./crawl.sh --backfill-runes`. Joined into
`stats._BASE` twice (alias `myr` on `me.puuid`, alias `oppr` on
`opp.puuid`) and surfaced as `runes` (mine) / `opp_runes` (opponent's) —
either `None` if not recorded — on every row from both `GET
/api/stats/games` and `stats.summary()`'s `recent`. The Overview tab's
"Recent games" table uses a ▸/▾ toggle per game (`.runes-toggle`,
`renderRecent`/`runesCompareCol` in app.js) that expands both players'
rune pages side by side, via the shared `runePageIcons()` (guide.js). The
Champ guide's own "Recent games" column (`recentGamesColumn` in guide.js)
still only shows your own runes inline, un-toggled — `opp_runes` is
available there too if that ever needs mirroring. Also joined (same
myr/oppr pattern) into `stats.block_games_detailed`, so a block game's
expanded per-game panel (`gameMetricsPanel` in blocks.js) shows the same
side-by-side `.runes-compare` layout via the shared `runesCompareCol()`
(app.js), reused as-is rather than duplicated.
`clips(id PK, owner_type CHECK IN ('session','block_game'), owner_id, label,
kind CHECK IN ('upload','link'), file_name, url, created_at_ms)` — 1-minute
video clips attached to a coaching session or a specific block game (not
the whole session/block — "specific parts"). `kind='upload'` stores the
file under `<db_dir>/clips/<uuid><ext>` (50 MB cap, `.mp4`/`.mov`/`.webm`/
`.m4v` only — `get_clips_dir()` in app.py, sibling to the sqlite file, so
it moves with `LOL_DB_PATH`/the packaged app-data dir); `kind='link'` just
stores a pasted URL (YouTube/Twitch/etc). API: `GET/POST /api/clips`,
`GET /api/clips/{id}/file` (serves the uploaded bytes), `DELETE
/api/clips/{id}`. Deleting the owning session/block/block_game cascades
to its clips *and* unlinks their files (`db.delete_clips_for_owner` /
`delete_clips_for_block`, called before the owning-row delete in app.py —
db.py never touches the filesystem, app.py does the unlink). Shared UI in
`app.js` (`clipsSection`/`wireClipsSection`, used by both the Coaching
progress session cards and `blocks.js`'s per-game stats panel) — clips
only fetch when that session/game is expanded, matching the rest of the
app's lazy-load convention.

## Development rules

- **All notes render as Markdown wherever they are displayed** — session
  notes, block learnings, block-game notes, matchup notes, champion notes,
  and any future note field. Use `renderNotes(...)` (vendored marked) inside
  an `md-body` element; never show raw/escaped note text in a read-only view.
- **Every user-facing feature adds an entry to `static/changelog.json`**
  (newest first; main functionality only, not tiny tweaks). It drives the 📋
  "What's new" panel; entries newer than the latest GitHub release tag show a
  "not yet released" badge.

- **Schema changes must be incremental and non-destructive.** Users upgrade
  the packaged app against a live database: new tables via
  `CREATE TABLE IF NOT EXISTS` (in `db.SCHEMA`), new columns via
  `ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check in
  `db._migrate`. Never `DROP`, recreate, or bulk-`DELETE`/`UPDATE` tables
  holding user content (sessions, block notes/learnings, matchup notes,
  champion notes, rank history) in a way that loses data — a primary-key
  change is the one case SQLite can't do via `ALTER TABLE`; the
  `matchup_notes` PK-widening migration is the template: rename to `_old`,
  create the new shape, copy every row forward, drop `_old`, all inside
  `_migrate`. One-time backfills must be idempotent and additive (see
  `seed_rank_history`). `tests/test_db.py::
  test_upgrade_from_older_db_preserves_all_notes` guards this — extend it
  when adding user-content tables.

## Testing conventions

- TDD: tests exist for every module; no network in tests (FakeClient /
  httpx.MockTransport / fake clocks).
- `tests/test_stats.py::add_match` is the canonical fixture builder — reuse it
  (test_app.py imports it) rather than writing raw inserts.
- App tests point `LOL_DB_PATH` at a tmp db via monkeypatch.

## Design docs

- Spec: `docs/superpowers/specs/2026-07-03-lol-topstats-design.md`
- Plan: `docs/superpowers/plans/2026-07-03-lol-topstats.md`
- Key user-visible assumption: rank grouping uses opponents' *current* rank
  (no historical rank exists in the Riot API) — flagged in README.
