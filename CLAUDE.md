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
  `.github/workflows/build.yml`.

- **Dev API key expires every 24 h.** 403 → `ApiKeyExpiredError`. Refresh at
  developer.riotgames.com, update `RIOT_API_KEY=` in `.env` (gitignored).
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
- `server/stats.py` — all aggregation in SQL over a filtered base query;
  matchup = tracked TOP player joined to enemy TOP participant; remakes
  (<300 s) excluded; opponent rank bucket `UNKNOWN` when not fetched.
- `server/app.py` — FastAPI; per-request sqlite connections; crawl runs in a
  daemon thread with module-level `CRAWL_STATE`; db path override via
  `LOL_DB_PATH` env (used by tests). "Hide my rank / LP" setting
  (`hide_my_rank`) redacts own-rank fields (`_MY_RANK_KEYS`) from ALL JSON
  API responses via middleware — new endpoints get hiding for free as long as
  they reuse those key names (`solo_*`, `start_ranks`, `end_ranks`); anything
  else rank-shaped needs its own check (see the rank-history endpoint). Session CRUD at `/api/sessions`;
  `/api/stats/progress` aggregates across ALL tracked puuids (no puuid param).
- Sessions have `title` + Markdown `notes` (legacy `note` column auto-migrates
  in `db._migrate`). `PATCH /api/sessions/{id}` edits them;
  `GET /api/sessions/export.md` produces the all-sessions Markdown export.
  Markdown renders client-side via vendored `static/vendor/marked.min.js`
  (no CDN at runtime; update by re-downloading from jsdelivr).
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
  = newest; block size is a setting (`db.get_block_size`, 1–10, default
  `db.BLOCK_SIZE`=3); complete = closed early, pool-snapshot stamped
  (finalized under an earlier size), or ≥size games;
  `db.add_game_to_block` auto-advances. Hydration via
  `stats.block_games_detailed`. API: `/api/pool`, `/api/blocks`,
  `POST /api/blocks/games` (409 names holding block),
  `GET /api/blocks/game-notes?opp_champion=` (read-only; feeds the matchup
  Block-notes section, `focusBlock(id)` in `blocks.js` deep-links a block
  card). UI in `blocks.js`; "+ Block" promote buttons on Recent-games and
  segment game rows.
- `static/` — no build step; state + fetch + innerHTML render in `app.js`;
  matchups view (own tab: per-matchup notes, expanded rows with Overview
  [win/loss strip + notes] / Games tabs) in `matchups.js`;
  trends view (SVG small-multiple charts + breakdown table) in `trends.js`;
  blocks view in `blocks.js`.

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
`matchup_notes(opp_champion PK, notes, updated_at_ms)` — per-matchup Markdown
notes (`GET /api/matchups/notes`, `PUT /api/matchups/notes/{champion}`; empty
notes delete the row).
`crawl_state(puuid+queue_id PK, newest_ms, complete)` — resume watermarks
`participant_metrics(match_id+puuid PK, has_challenges, one REAL col per
metric key)` — coaching metrics, tracked players only, columns generated
from `server/metrics.py`

## Development rules

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
  rank history). One-time backfills must be idempotent and additive (see
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
