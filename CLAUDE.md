# CLAUDE.md

**Coach Potato** — coaching & improvement app for LoL: crawls Riot match
history for the configured accounts into sqlite, serves matchup stats /
coaching progress / trends / block learnings via FastAPI + vanilla-JS
frontend. Stats cover games in ANY role: `stats._BASE` keys off the tracked
player's own `team_position` (`me.team_position != ''`) and matches the lane
opponent as the enemy in that SAME role (`opp.team_position = me.team_position`).
(It used to be hard-scoped to `'TOP'`; that filter was removed so all roles show.)

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
  static:static`) produce the packaged build; it starts pywebview with
  `private_mode=False` + `storage_path=<db_dir>/webview` (`desktop.webview_storage_path`)
  — pywebview's DEFAULT private mode discards localStorage on close, which
  silently reset every saved UI preference (column pickers, collapsed
  blocks, skill grids, per-section last view) on each launch; CI matrix in
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
  `reportlab` was added for the Matchup guide's PDF export
  (`server/pdf_export.py`) — pure-Python-installable prebuilt wheels on all
  three OSes, builds clean under PyInstaller like the other compiled/native
  deps here.
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
  paths, and numeric match-v5 ids) that drives the Matchup guide rune-page
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
  (tree/rune/shard ids) into the same rune-page shape as the matchup guide.
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
  `_filtered_base` takes a `side` filter ("blue"=team 100 / "red"=team 200,
  via `_SIDE_TEAM`) threaded through matchups/summary/progress_segments/
  segment_metrics/trend_buckets/games_in_range; endpoints read `?side=` (in
  `stat_filters` for matchups/summary, explicit elsewhere). Frontend: a Side
  select in the Overview/Matchups/Trends/Progress filter rows.
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
  `--ui-opacity` (set on `:root` from JS, raw 0.2–1) which is remapped into a
  floored `--surface-opacity` (`calc(0.6 + 0.4 * var(--ui-opacity))`, i.e.
  [0.68, 1]) that drives `--surface-1`/`--page` through `color-mix(...)` —
  every existing `var(--surface-1)`/`var(--page)` usage becomes translucent
  for free, no per-component changes. The floor keeps text-bearing surfaces
  (cards, tables, the Matchup guide) readable over a background image while
  still leaving "some glass" — a decorative surface that wants the raw,
  unfloored opacity should reference `var(--ui-opacity)` directly. Plus a
  fixed, full-viewport `#bg-image` div (z-index -1) behind everything for the
  picture itself. `accent_color` (optional `#rrggbb` hex, `HEX_COLOR_RE`
  validated; `None`/unset = theme default) overrides `--series-1` the same
  way — set inline on `:root` from JS, removed to fall back to the
  stylesheet's light/dark default. `--accent-wash` derives from `--series-1`
  via `color-mix(...)` too, so every accent-tinted background/border (active
  tabs, buttons, chip highlights) follows a custom color for free — watch
  for new one-off `rgba(42, 120, 214, ...)`-style literals bypassing this.
  `default_champion` (optional, `_validate_champion`d if given) is
  preselected as "My champion" on the Matchup guide (`guide.js`'s
  `loadGuideChampionOptions`, ahead of the "first played champion"
  fallback) — set on `state.defaultChampion` from `/api/settings` at app
  init and again after a settings save, so it applies without a reload.
  Session CRUD at `/api/sessions`;
  `/api/stats/progress` aggregates across ALL tracked puuids (no puuid param).
- Sessions have `title`, optional `coach`, and Markdown `notes` (legacy `note`
  column auto-migrates in `db._migrate`; `coach` is an additive column).
  The Coaching-progress view is two panes — Progress (segment table) and
  Sessions (the log) — switched by `#progress-tabs`/`setProgressTab`, opening on
  the `progress_default_tab` setting ("progress"/"sessions") and then staying
  wherever the user last put it. Adding AND editing a session both go through
  one popup (`openSessionModal`/`renderSessionModal`, the shared
  `#modal-overlay` shell); there is no inline session editor any more, which is
  why `coach` is backfillable on old sessions. The `coaches` table is ONLY the
  autocomplete list: `db.remove_coach` stops suggesting a name and never edits
  the sessions that recorded it, and it sets `coaches_curated=1` so
  `db.seed_coaches` (which backfills the list from existing sessions on connect,
  for upgrades and restored backups) stops re-adding what you removed. `PATCH /api/sessions/{id}` edits them;
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
  (labels/groups/agg kinds/directions/`default_hidden`/`signed`) driving the
  `participant_metrics` DDL, payload parsing, SQL aggregation and frontend
  meta. Stored for tracked players only; crawler captures on insert;
  `crawler.backfill_metrics()` / `./crawl.sh --backfill-metrics` re-fetches
  older matches. Adding a metric grows `participant_metrics` via the
  additive column loop in `db._migrate` (CREATE TABLE IF NOT EXISTS won't
  alter an existing table). `stats.segment_metrics` (per period) and
  `stats.trend_buckets` (day/week/month; week = Monday date) feed
  `/api/stats/metrics` and `/api/stats/trends` (both include `meta`);
  `/api/metrics/meta` returns the registry alone (for the per-view metric
  column pickers). Metrics with `source="timeline"` (lane ΔCS/ΔLevel/ΔGold
  vs the direct lane opponent at ~7 & ~14 min) come from the match-v5
  TIMELINE, not the detail payload: `metrics.parse_timeline_deltas` reads
  the frame nearest each mark; the crawler fetches the timeline per new
  match (`Crawler._safe_timeline`, tolerant of 404/failure) and merges the
  deltas, setting `has_timeline=1`. `crawler.backfill_lane_deltas()` /
  `./crawl.sh --backfill-lane-deltas` fills existing rows (has_timeline=0)
  using only the timeline + stored participants for the lane opponent, via
  `db.update_participant_timeline` (which never clobbers challenge metrics).
  These six are `default_hidden`.
- **Strong side / weak side** rides on the SAME timeline fetch (no extra API
  calls). `metrics.parse_jungle_starts` reads each jungler's position at the
  earliest frame where they've cleared a camp (`jungleMinionsKilled >= 1`,
  else the ~60s frame — never frame 0, where everyone is in the fountain ON
  the mid diagonal and the test is meaningless) and maps it to a half via
  `metrics.map_half` (`'top' if y > x`). The halves are ABSOLUTE, so a team's
  jungle start can be compared straight against a lane half regardless of
  side. `metrics.strongside(position, half)` applies the rule — you are strong
  side when your jungler started OPPOSITE your lane — and returns None for
  MIDDLE/JUNGLE (`metrics.LANE_HALVES`) or an unknown start. Junglers are
  identified from stored participants (`db.jungler_puuids`) since the timeline
  carries no roles; `crawler._store_jungle_starts` writes both teams via
  `db.set_match_jungle_starts`. `backfill_lane_deltas` fills it opportunistically
  from its own timeline fetch, so `crawler.backfill_jungle_starts()` /
  `./crawl.sh --backfill-jungle-sides` is only needed for matches whose
  timeline was already processed before this existed.
  `stats.block_games_detailed` derives `auto_strongside` /
  `opp_auto_strongside` (+ `my_jungle_half`/`opp_jungle_half` for the UI hint)
  in `_add_jungle_sides`; the lane counterpart shares the lane half, so their
  verdict is the same comparison against the ENEMY jungler's start. It
  measures the START, not where the jungler actually spent the game.
  Across the app the pattern is: EXPANDED
  per-game/segment stat panels always show ALL metrics (no picker), and each
  aggregate TABLE has a column picker whose metric-average columns start off.
  Matchups (`muAllCols`/`cp-mucols`, base cols default on) and coaching
  progress (`progressAllCols`/`cp-cols-progress`) get per-opponent /
  per-segment metric averages from `stats.matchups`/`progress_segments`
  (both now return a `metrics` dict via `_metric_agg_select`); blocks expose
  the deltas as `BLOCK_COLS`; trends' breakdown/charts use
  `renderMetricColPicker` (`cp-metriccols-trends`). The lane deltas render
  as averages in these tables. The web app deepens block-game stats
  proactively: `_run_crawl` calls `backfill_lane_deltas(block_games_only=True)`,
  and opening Blocks fires `POST /api/blocks/backfill-timelines` (background
  thread `_run_timeline_backfill` → `TIMELINE_STATE`, guarded by
  `_riot_job_running()` so it never runs alongside a full crawl and
  double-drives the rate limiter). `block_games_detailed` returns
  `has_timeline` per game; blocks.js shows a ⏳ marker on games still being
  fetched and polls `/api/blocks/timeline-status`.
- Block series: `block_series(id, title, goals, created_at_ms)`; every `blocks`
  row has a `series_id` (added in `_migrate`; `seed_block_series` on connect
- Full-game curve: `participant_frame_series(match_id+puuid+minute, cs, xp,
  gold, level)` — the WHOLE per-minute timeline series (not just the two
  ~7/14 min marks lane deltas sample), stored for ALL 10 participants per
  match (like `participants` itself), so any pair's curve can be charted
  without resolving "who is the lane opponent" at storage time.
  `metrics.parse_frame_series(timeline_json)` is the pure extraction
  function (buckets each frame's `timestamp` into `minute = round(ts_ms /
  60000)`, one entry per participant per frame); `Crawler._store_frame_series`
  writes it via `db.insert_frame_series` (INSERT OR IGNORE, idempotent).
  Populated once per match — not once per tracked puuid — from the SAME
  timeline fetch `crawl_player` already makes for lane deltas (no extra API
  call). `crawler.backfill_frame_series()` / `./crawl.sh
  --backfill-frame-series` fills existing matches that already had a
  timeline processed (`participant_metrics.has_timeline=1`) but predate this
  feature; it re-fetches the timeline (the raw JSON isn't cached anywhere)
  so a permanently-missing timeline can resurface on repeat runs — accepted
  since this is a manual, on-demand backfill, not part of the crawl loop.
  `stats.game_curve(conn, match_id, puuid, opp_puuid=None)` reads it back as
  `{minutes: [...], me: {cs, xp, gold, level}, opp: {...} | null}`, behind
  `GET /api/stats/game-curve?match_id=&puuid=&opp_puuid=` (404 when nothing
  recorded for that puuid). UI: the Overview "Recent games" table's ▸/▾
  "Curve" toggle (`curveUi`/`toggleGameCurve`/`gameCurveSection` in app.js,
  a sibling to the existing `.runes-toggle`, since curve data needs an async
  fetch on first expand while runes are already inline on the row) renders
  gold + CS as two small SVG line charts (`gcChartSVG`) — the two most
  immediately useful for a lane/game read; XP/level are in the payload for a
  future metric switcher. A second `--series-2` theme color (in
  `style.css`, alongside `--series-1`) distinguishes the opponent's line.
- Block series: `block_series(id, title, created_at_ms)`; every `blocks` row
  has a `series_id` (added in `_migrate`; `seed_block_series` on connect
  ensures ≥1 series exists and assigns orphan/legacy blocks to it).
  `create_block` attaches the current (newest) series; `start_new_series`
  (POST `/api/blocks/series`) opens a fresh one, finalizing an in-progress
  non-empty block or absorbing an empty one so the next game starts clean.
  `goals` is Markdown (what a two-week challenge is FOR), edited via
  `PATCH /api/blocks/series/{id}` (partial — title and goals are written
  independently so the two editors can't clobber each other).
  `/api/blocks` returns `series` (all rows, newest first) + `current_series_id`
  INDEPENDENTLY of the blocks: a series is described even when it holds no
  blocks yet, which is what makes a just-started series visible before its
  first game (it used to be derivable only from a block's `series_title`).
  `closing_notes` is the end-of-series retrospective (did the goals land, what
  actually changed), same partial-update rules.
  The **Series view** (`#series-view`, `initSeriesView`/`renderSeriesView` in
  blocks.js, under Coach) is the whole arc of a challenge on one page: goals →
  each block's learnings (read-only there; they're edited on the block itself,
  the title links across via `focusBlock`) → closing notes. It reuses
  `/api/blocks` rather than adding an endpoint — that payload already carries
  every series plus every block's learnings.
  UI: the ACTIVE series sits on the champion-pool line (`#series-current` in
  the `#pool-summary` panel, `renderCurrentSeries` in blocks.js) — the one row
  always on screen. Goals live in a popup (`openSeriesModal`/
  `renderSeriesModal`, reusing the shared `#modal-overlay`/`#modal-box` shell
  and `closeModal()` from cooldowns.js), opened by the ✎ next to the active
  series or by a block's series bubble — the bubble is what keeps an OLDER
  series' goals reachable once a newer series is current. Title is editable in
  the same popup.
  Block header label is series-title + a less-prominent per-series index
  when the `block_series_enabled` setting is on (default), else a bare
  continuous `#global_index`. BOTH indices are positional/gapless (computed
  in `_blocks_payload`, not `blocks.id`) — deleting then recreating a block
  never skips a number. `/api/blocks` returns `series_title`/`series_index`/
  `global_index` per block + top-level `series_enabled`. Block header: the
  editable per-block title on the left (placeholder = the block's date, from
  its earliest game) with the muted `#index` beside it, series shown as a
  right-aligned bubble, and the current block tinted (`.block-current`); the
  active series name sits by the "+ New series" button. Dates/times render
  via `fmtDate`/`fmtTime` per the `date_format` setting (`iso` default / `us`
  / `eu`; us=12h, iso&eu=24h; `state.dateFormat`).
- Block learnings: `champion_pool` (role main_blind/core/counter, replaced
  wholesale, `sort` column = user-set priority order via drag'n'drop chips;
  the EDITOR lives in Settings — `#pool-card`, wired by `initSettings`,
  functions in blocks.js — while the Blocks view shows a read-only summary
  with a ✎-to-Settings shortcut), `blocks` + `block_games` (UNIQUE
  match_id+puuid). Current block
  = newest; block size is a setting (`db.get_block_size`, >=1, no upper
  bound, default `db.BLOCK_SIZE`=3); complete = closed early, pool-snapshot stamped
  (finalized under an earlier size), or ≥size games;
  `db.add_game_to_block` auto-advances. Time-gap auto-close: adding a game
  whose game time is > `block_gap_hours` (setting, default 3 h, 0 = off)
  from the open block's latest game 412s with `{"reason": "gap"}` for
  client confirmation (skipped when `block_gap_confirm` is off), then
  closes the block (`db.block_gap_exceeded`). Each `block_games` row also
  carries manual, user-set flags edited in the game's expanded stats panel
  (`blocks.js`): `weakside` (nullable bool — Strongside/Weakside, a note that a
  lane deficit was expected because you weren't the jungle-prioritised lane;
  it is the OVERRIDE for the auto-detected side, so leaving it NULL means
  "use Auto" — see the strong/weak-side detection below)
  and `lane_result_7`/`lane_result_14` (nullable text each, one of
  `db.LANE_RESULT_VALUES` = stomped/lost/even/won/stomp — overrides that mark's
  lane column, graded independently per mark since a game can read differently
  at 7m vs 14m, e.g. a losing all-in that pays off by the second read).
  All are additive columns, partial-updatable via
  `PATCH /api/blocks/games/{id}` (each field set independently, so none
  clobbers the notes or the other mark). Hydration via
  `stats.block_games_detailed`. API: `/api/pool`, `/api/blocks`,
  `POST /api/blocks/games` (409 names holding block),
  `GET /api/blocks/game-notes?opp_champion=` (read-only; feeds the matchup
  Block-notes section, `focusBlock(id)` in `blocks.js` deep-links a block
  card). UI in `blocks.js`; "+ Block" promote buttons on Recent-games and
  segment game rows. A block game's Clips section (see `clips` table below)
  lives in its per-game stats panel, loaded together on first expand.
- **Top nav is two rows, driven by `NAV_SECTIONS` in `app.js`** — sections
  (Analyze / Coach / Prepare) on the first row, the active section's views on
  the second (`#main-view-toggle` / `#sub-view-toggle`, both rendered by
  `renderNav()`, both empty in the markup). Adding a view = one line in
  `NAV_SECTIONS` + its `VIEW_LABELS` entry + a `<div id="<key>-view">` + an
  init call in `setMainView` — no nav markup to touch, and it gets
  hash deep-linking, Settings' visible-tabs checkbox behaviour and the section
  bars for free. A section auto-hides when all its views are hidden
  (`sectionVisible`); entering a section lands on the view you last used there
  (`sectionEntryView`, localStorage `cp-nav-last-<section>`); `1`/`2`/`3`
  switch sections (ignored while typing or with a modal open). Settings is
  deliberately NOT a section — it stays the ⚙ icon, `state.mainView ===
  "settings"` with the sub-bar still showing the section you came from.
  The view key is the URL hash (`#blocks`, `#tiers`, …); `overview` is `#`.
  NOTE the display name of the `guide` view is **Playbook** (renamed so it
  stops colliding with the Matchups stats tab) — the key, hash, file
  (`guide.js`) and API paths are all still `guide`/`matchups/notes`.
- Champion pool has its own view (`#pool-view`, `initPool` in blocks.js) under
  Coach; it used to be a field inside Settings, which still links to it.
- `static/` — no build step; state + fetch + innerHTML render in `app.js`;
  matchups view (own tab: expanded rows with Overview [win/loss strip + block
  notes] / Games tabs; a 📖 link per row — shown only when a specific "My
  champion" filter is active, since guides are scoped per champion pair —
  deep-links to that matchup's Matchup guide) in `matchups.js`;
  Player comparison ("you vs them") is scoped, NOT matchup-only:
  `GET /api/comparison?scope=matchup|champion|overall` returns `you` (all
  tracked puuids aggregated, so it matches how coaching progress treats you)
  plus one entry per enabled comparison player, every side built by the same
  `stats.comparison_entry` so the two are measured identically. Each entry is
  `{scoped, overall, recent}` — `overall` is the champion baseline and is None
  unless the scope is a matchup. `openComparison({my, opp, scope})` in guide.js
  is the single entry point (Playbook rows, Matchups rows, My champions rows,
  the Coaching-progress button); it prefers a real second native window via
  `DesktopApi.open_compare` (pywebview), then `window.open`, then an in-app
  overlay. `compare.html` renders you first and has two layouts — a stat
  matrix (default; rows = stats, columns = players, best-in-row marked
  direction-aware) and the original card grid behind "Compact view"
  (`cmp-compact` in localStorage).
  Live Lookup (📡 on the Matchup guide) calls `GET /api/live-game`
  (`riot_client.get_active_game` → spectator-v5 by-puuid on the platform
  host, per tracked account; 404 = not in a game) which returns the numeric
  championIds in the game; the client maps them via DDragon champion.json
  (`loadChampionKeyMap`), switches the guide to your champion, guesses the
  lane opponent from matchup history (spectator has NO role/lane, so the
  most-faced enemy is the pick) and opens the One Pager with a 5-enemy
  switcher to correct it. a 📄 button per Matchup-guide row opens the One Pager
  (`openOnePager` in guide.js, `#onepager-overlay` full-screen opaque
  overlay): a second-screen quick reference for one matchup — runes, skill
  order, item build, matchup + general notes; deliberately NO history/
  stats. Esc/✕ closes.
  a ⏱ button per matchup row (also on Matchup-guide rows) opens the
  cooldown-comparison modal (`cooldowns.js`: generic
  `#modal-overlay`/`#modal-box` shell in index.html; spell data fetched
  from DDragon `champion/<id>.json` at open time, session-cached; two view
  modes ["At level": per-side level slider + per-spell table; "Level
  matrix": sides stacked, the skill grid's active cells become value
  bubbles showing each ability's haste-reduced cooldown from that level on
  — `cp-cd-view` in localStorage]; both share an 18×4 skill-order grid
  [level × Q/W/E/R, click to spend that level's point — delegated listener
  on #modal-box, since matrix-mode haste edits re-render the grid;
  validated client- AND server-side — basics max 5, point k needs level
  2k-1, R at 6/11/16] + freeform haste sources; reduced cd =
  base/(1+haste/100), R adds ult haste. Grid persists per champion in
  localStorage `cp-skill-grid-<champ>` [legacy `cp-skill-order-<champ>`
  priority lists auto-convert]; "Save build to matchup guide" writes it to
  `matchup_notes.skill_order` for the open matchup — saved builds preload
  on open and display read-only in the guide row via `skillGridMini`.
  `PUT /api/matchups/notes/{my}/{opp}` is a PARTIAL update: only body keys
  are written, so popup and guide editor don't clobber each other's
  fields; `db.set_matchup_note` keeps un-passed fields via a _KEEP
  sentinel);
  trends view (SVG small-multiple charts + breakdown table + an expandable
  "Death map" heatmap — see `player_map_events` below) in `trends.js`;
  blocks view in `blocks.js`; Matchup guide view (own nav tab: pick "My
  champion" from the full roster — not just played champions — see/edit
  general champion notes, full rune pages + patch + notes for every matchup
  it has faced, or add one for a matchup not yet played via the shared
  champion-roster autocomplete from `blocks.js`; each matchup's "Recent
  games" column shows real games with the actual runes played, when
  recorded; Export/Import menus export/import one champion's whole guide as
  JSON, optionally password-encrypted) in `guide.js`; Research view (own nav
  tab: a study journal for other players' games, unrelated to the tracked-
  account crawler — freeform entries: player name, optional champion/
  opponent, one Markdown notes field (covers general + VOD notes together),
  and multiple screenshots — deliberately no timestamp log or video/clip
  attachments here) in `research.js`.
  attachments here) in `research.js`; Macros view (own nav tab: a flat list
  of collapsible (collapsed by default) freeform title + Markdown-notes
  sections for game-macro notes — not tied to any champion, matchup, or
  session; sections append at the bottom in creation order, no drag-reorder;
  editing a section force-expands it and keeps it expanded after save) in
  `macros.js`.
- **Ascent VOD integration** — `server/recordings.py` imports local recordings
  from Ascent's own sqlite db (`%LOCALAPPDATA%\Ascent\recordings.db`, path
  overridable via the `ascent_db_path` setting). Ascent stores `game_match_id`
  per recording — the SAME match-v5 id we key `matches` on — which is the whole
  reason this can work without guesswork. **Two invariants the module exists to
  hold:** Ascent's db is only ever READ, and always via a snapshot (db + `-wal`
  + `-shm` copied to a temp dir) because it is a live database owned by another
  running process with a large WAL; and video files are never copied, moved or
  deleted — only paths are stored, and "forget" drops the row, not the file.
  Only recordings whose match is already crawled are imported. `sync()` runs at
  the end of `_run_crawl` (best-effort — Ascent missing must never fail a crawl)
  and behind `POST /api/recordings/sync`. `recordings` table is keyed by
  Ascent's uuid so re-syncing is idempotent; re-sync refreshes path/timings but
  never clobbers OUR columns (`offset_ms`, YouTube state).
  `GET /api/recordings/{uuid}/file` streams the local mp4 through
  `FileResponse` (Range requests work, which is what makes seeking possible) and
  only serves paths already in the table — the id is looked up, never taken from
  the request. `player_map_events` was widened from deaths-only to also carry
  kills/assists/towers/inhibitors/objectives (`metrics.parse_map_events`), with
  a `detail` label whose leading `-` marks an event that went AGAINST the
  player's team. SQLite can't ALTER that CHECK constraint, so `_migrate`
  rebuilds the table (rename/create/copy/drop), carrying every existing death
  forward. **Anything reading that table for the heatmap must filter
  `event_type='death'`** — `stats.map_events` now does, or the Trends death map
  would start plotting kills and towers. Existing rows look complete
  (`has_map_events=1`) but hold only deaths, so
  `backfill_map_events(recompute=True)` / `./crawl.sh --recompute-map-events`
  re-derives them; it re-fetches every timeline (real Riot calls) and, like the
  lane-delta recompute, skips rather than clobbers when a fetch fails.
  **A second, offline event source** lives in `server/ascent_log.py`: Ascent
  records via Overwolf's GEP, which reads League's Live Client Data feed, and
  those events are written into `%LOCALAPPDATA%\Ascent\logs\*.log` inside
  periodic game snapshots (`match_id="..."` GEP lines bracket each game;
  `"allPlayers":[...]` gives team membership, `"events":[...]` the feed). Parsed
  into the same rows with `source='ascent_log'` and **NULL x/y** — Live Client
  Data has no coordinates, so `stats.map_events` filters `x IS NOT NULL` as well
  as `event_type='death'`. Needs no API key, but only reaches as far back as the
  logs roll (days). Team membership decides ours-vs-theirs; without it every
  objective a teammate took would read as lost. **The two sources must never be
  mixed for one game** — both describe it, so reading both doubles every death.
  `recordings.preferred_source()` picks whichever has MORE events per
  (match, puuid): a pre-widening timeline holds deaths only and loses to the
  log, a recomputed timeline wins. Self-correcting, no flag to maintain.
  `recordings.timeline_markers()` reads them all back for the VOD chapter list
  and for the mini-map beside the player (`recordingMap` in app.js; each dot
  seeks the video to that moment). The Rift itself is `riftBackdrop()` in
  app.js, shared by that map AND the Trends death map so there is only one:
  Riot's official minimap (`ddragon .../img/map/map11.png`, hotlinked exactly
  like the champion/rune/item icons the app already uses) laid over a
  hand-drawn schematic fallback (`riftSchematic()` — lanes, river, turret line,
  Baron/Dragon pits) that shows through if the image can't load. Both are drawn
  in Riot's map coordinates and projected with `heatmapPoint`, so no offset
  correction is needed — verified by plotting known landmarks (Baron pit, both
  nexuses) over the image.
  `death_markers()` turns `player_map_events` timestamps into video
  positions: measured across a real library, Ascent starts recording within a
  few seconds of the game (durations match to ~2-6s), so timeline time maps
  essentially 1:1 onto video time; `offset_ms` is the per-recording nudge.
  Frontend: `recordingSection`/`wireRecordingSection` in app.js, the same
  shared-component pattern as `clipsSection`/`reflectionSection`, used by
  Overview's Recent games and blocks.js's per-game panel.
- **YouTube upload** — `server/youtube.py`, behind
  `POST /api/recordings/{uuid}/youtube` (background thread + `UPLOAD_STATE`,
  polled via `/api/recordings/upload-status`, mirroring `CRAWL_STATE`). Needs
  the user's OWN Google OAuth desktop client (`youtube_client_secrets` setting)
  — that cannot be automated, it is tied to their Google account.
  **Entirely optional and NOT installed by default**: the Google libraries live
  in `requirements-youtube.txt`, not `requirements.txt`, so neither a dev setup
  nor the packaged build pulls the Google API stack. They're imported lazily,
  and `youtube.libraries_available()` feeds `has_credentials()` — so with the
  libraries absent the app reports `youtube_ready: false` and the UI offers the
  manual "reveal the file" path instead of an upload button that could only
  fail. Keep that invariant if you touch the readiness logic; the
  `test_youtube_is_optional_without_the_google_libraries` test pins it, and the
  two tests that genuinely need the libraries skip without them.
  Token caches as `youtube_token.json` beside the db. Defaults to **private** because
  an unaudited OAuth project cannot produce anything else regardless of the
  setting, and the default quota allows only ~6 uploads/day. The UI always
  `confirm()`s before uploading — it publishes to the user's channel.

## Schema (data/lol.sqlite)

`players(puuid PK, game_name, tag_line, is_tracked, solo_tier/division/lp, rank_fetched_at_ms)`
`matches(match_id PK, queue_id, game_creation_ms, game_duration_s, game_version,
crawled_at_ms, jungle_start_100, jungle_start_200)` — the two `jungle_start_*`
columns hold the map half each team's jungler started in, for strong/weak-side
detection: `'top'`/`'bot'`, `''` when the timeline was read but no jungler could
be identified, `NULL` when never looked at (that distinction is what stops
`backfill_jungle_starts` re-fetching jungler-less matches forever). Note the
`matches` DDL keeps its comments OUTSIDE the parens — SQLite re-parses a stored
CREATE TABLE on `ALTER TABLE ... DROP COLUMN`, and an inline `--` comment
truncates it ("incomplete input"); `tier_lists` still has an inline one.
`participants(match_id+puuid PK, champion_name, team_id, team_position, win, k/d/a, cs, gold_earned, damage_to_champions, riot_id_name, summoner1_id, summoner2_id, items)`
— `summoner1_id`/`summoner2_id` are match-v5 summoner-spell ids; `items` is a
JSON array of the 7 final inventory slots (item0..item6; slot 6 = trinket,
0 = empty — final positions, NOT purchase order). All three are nullable
(added later; existing rows are NULL until re-crawled). Parsed in
`parsing.py`, stored on every participant by `insert_match`; surfaced (for the
`me` side) via `stats._BASE` as `spell1`/`spell2`/`my_items_json` and consumed
by the Matchup-guide comparison (`comparison_for_matchup` recent games →
compare.html / guide.js: per-game summoner spells + first 3 items). Because
`insert_match` is INSERT OR IGNORE, a re-crawl never fills these on an existing
row — `crawler.backfill_items()` / `./crawl.sh --backfill-items` re-fetches
match detail to populate them (via `db.update_participant_loadout`), mirroring
`backfill_runes`. Newly-added comparison players get them automatically (their
games are new to the db).
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
updated_at_ms)` — "Matchup guide" scoped per (your champion, opponent
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
Those `my_champion=''` rows are unreachable from the guide UI, so Settings
shows an "Older matchup notes" section (only while such rows exist —
`refreshLegacySection` in app.js) with Migrate (a champion select; `POST
/api/matchups/legacy-notes/migrate` reassigns the rows to that champion,
skipping opponents it already has a guide for and reporting them back as
`skipped`) and Delete (`DELETE /api/matchups/legacy-notes`, confirm()ed —
the one user-content delete that's allowed, since it's explicit). `GET
/api/matchups/legacy-notes` returns {count, notes}. `patch_version` is
validated everywhere it's written (`PATCH_VERSION_RE`, e.g. 16.14 or
16.14.1, or empty); the guide editor offers a patch dropdown built from
DDragon versions.json (cached in `state.ddragonVersions` by
`loadDdragonVersion`, major.minor deduped), defaulting to the current
patch. "Your champion" defaults (Matchup guide pre-selection, cooldown
popup, legacy-migrate select) come from the champion pool's first entry —
`poolChampionOrder()` in app.js (flattened main→core→counter, cached,
reset on pool save); use it for any future your-champion-scoped feature.
A `default_champion` settings key existed briefly (v1.25–v1.30) and may
linger in old dbs — it's ignored.
`champion_notes(champion PK, notes, updated_at_ms)` — general (non-matchup)
Markdown notes for a champion, shown above the matchup list on the Champ
guide page. `GET`/`PUT /api/champions/notes/{champion}`.
`champion_item_builds(champion PK, core, situational, updated_at_ms)` — a
mobafire-style item build, separate from the freeform general notes:
`core` is a JSON array of item names (an ordered "first N items" build,
capped at `MAX_CORE_ITEMS`=6); `situational` is a JSON array of `{label,
items}` (a labeled alternative, e.g. "vs heavy AP", capped at
`MAX_ITEMS_PER_SECTION`=5 items, `MAX_SITUATIONAL_SECTIONS`=12 sections).
Items are stored as plain name strings, not ids — the server does no item
validation beyond shape/counts (`_validate_item_build` in app.py, shared by
the PUT endpoint and champ-guide import). `GET`/`PUT
/api/champions/item-build/{champion}`. Icons are resolved entirely
client-side: `guide.js`'s `loadItemData()` fetches DDragon's current
`item.json` once (like `loadDdragonVersion()`, cached in localStorage per
version) filtered to Summoner's Rift-purchasable items, building a
`name -> icon` map for the picker and for rendering existing builds — no
static `items.json` asset, unlike champions/runes, since items change
essentially every patch. UI in `guide.js` (`itemBuildBlock`/
`renderGuideItemBuild`): an edit/view toggle like general notes, a
searchable icon picker shared between the core list and every situational
section (`itemPickerTarget` tracks which list is receiving the next pick).
Matchup guide export/import (`server/crypto.py`): `POST /api/matchups/notes/
export` bundles one champion's `champion_notes` + `champion_item_builds` +
all its `matchup_notes` rows into a downloadable JSON file; an optional
`password` in the request body encrypts the payload (PBKDF2-HMAC-SHA256
key derivation + Fernet/
AES-128 via the `cryptography` package — a real cipher, not obfuscation).
`POST /api/matchups/notes/import/preview` decrypts (if needed) and reports
which opponents would be added/overwritten without writing anything;
`POST /api/matchups/notes/import` performs the writes. Wrong/missing
password on an encrypted file → 401. UI in `guide.js` (Export/Import menus
on the Matchup guide page); import always shows the preview's overwrite
count in a `confirm()` before committing.
`GET /api/matchups/notes/export.pdf?my_champion=` — a printable PDF
mirroring the whole Matchup guide page (general notes, item build, and every
matchup: patch, rune pages, Markdown notes) except the "Recent games"
column, which is deliberately left out — built by `server/pdf_export.py`
(reportlab). Unlike the JSON export, this fetches icons live at export
time and embeds them — slower, needs network, no password option (nothing
to protect in a print-and-read document). Rune/tree/shard icons use the
same version-independent CDN paths guide.js hotlinks (name→icon lookups
added to `rune_data.py` as `TREE_ICON`/`RUNE_ICON`/`SHARD_ICON`); item
icons are version-scoped, so `_IconFetcher` additionally fetches the
current ddragon version + `item.json` once per export call (mirroring
guide.js's `loadItemData()`, server-side) to resolve item names to icon
files. A fetch failure for one icon (or the version/item-data lookups)
silently skips just that icon rather than failing the export.
Markdown notes go through a small purpose-built converter
(`_markdown_flowables`/`_inline_markup`) — headings/paragraphs/bullets/
**bold**/*italic*/`code` only, no tables/links/nested lists, since coaching
notes here are short freeform text, not full documents. Uses reportlab's
core Helvetica font (Latin-1/WinAnsi only) — non-Latin note text won't
render correctly; embedding a Unicode font was judged out of scope.
`crawl_state(puuid+queue_id PK, newest_ms, complete)` — resume watermarks
`participant_metrics(match_id+puuid PK, has_challenges, has_timeline, one
REAL col per metric key)` — coaching metrics, tracked players only, columns
generated from `server/metrics.py`; `has_timeline` marks that the match
timeline was processed (lane-delta columns filled, or blank if no opponent/
timeline) so the backfill skips it
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
Matchup guide's own "Recent games" column (`recentGamesColumn` in guide.js)
still only shows your own runes inline, un-toggled — `opp_runes` is
available there too if that ever needs mirroring. Also joined (same
myr/oppr pattern) into `stats.block_games_detailed`, so a block game's
expanded per-game panel (`gameMetricsPanel` in blocks.js) shows the same
side-by-side `.runes-compare` layout via the shared `runesCompareCol()`
(app.js), reused as-is rather than duplicated.
`player_map_events(id PK, match_id, puuid, event_type CHECK IN ('death'), x,
y, timestamp_ms)` — death locations for the Trends "Death map" heatmap,
decoded from the match-v5 TIMELINE's `CHAMPION_KILL` events
(`metrics.parse_death_events`, victim's participant id resolved the same way
`parse_timeline_deltas` does) for tracked + comparison puuids only (mirrors
`Crawler._stored_puuids()`'s scope — a personal reflection tool, not full-
match analysis). **Deaths-only**: match-v5's `WARD_PLACED` events do NOT
carry a position field — confirmed via a live timeline fetch (a Riot dev key
in this repo's `.env` had expired, so verification instead relied on Riot's
own developer-relations tracker, which has an open, unresolved feature
request asking for one) — so `event_type` only ever stores `'death'`; the
column is left generic rather than named `death_events` in case Riot ever
adds ward positions. Wired into the crawl exactly like the lane-delta
metrics: `Crawler._store_metrics` calls `parse_death_events` off the SAME
timeline fetch used for lane deltas and stores rows via `db.replace_map_events`
(delete-then-insert, idempotent), setting `participant_metrics.has_map_events=1`
(an additive column, same idiom as `has_timeline`) so `Crawler.backfill_map_events()`
/ `./crawl.sh --backfill-map-events` (mirrors `backfill_lane_deltas`, incl.
`block_games_only`) skips already-processed matches; a missing/failed
timeline still marks the row done with zero events so it isn't retried
forever. `stats.map_events(conn, puuids, from_ms=, to_ms=, champion=,
roles=)` joins `player_map_events` onto the same `_filtered_base` every other
Trends-style query uses, behind `GET /api/stats/map-events?champion=&role=&
from_ms=&to_ms=` (or `range=`/`from=`/`to=` like `/api/stats/games`).
Frontend: an expandable "Death map" section at the bottom of the Trends view
(`trends.js`, collapsed by default, `.seg-toggle`, data fetched lazily on
first expand and invalidated whenever a filter changes) draws the Rift via the
shared `riftBackdrop()` (app.js) and plots
each death as a semi-transparent `--critical`-tinted dot (overlapping dots
naturally read as density). Riot's timeline coordinate space is ~0–14820 (x)
/ ~0–14881 (y), origin bottom-left; `heatmapPoint()` flips the y-axis into
the SVG's top-left-origin viewBox. Trends gained its own Period range-preset
row (`#trend-range-presets`, same `.preset`/`data-range` pattern as
Matchups') and a Role select (`#trend-role`, wired into the same shared
`state.roleFilter`/`syncRoleSelects()` as the Overview/Matchups role filters)
purely to drive this — `stats.trend_buckets` also gained optional
`from_ms`/`to_ms` (additive; `None` = full history as before) so the
existing bucket charts/breakdown table honor the new Period filter too.
`participant_frame_series(match_id+puuid+minute PK, cs, xp, gold, level)` —
full-game per-minute gold/CS/XP/level series from the match-v5 timeline, for
ALL 10 participants (not just tracked/lane-opponent puuids, unlike
`participant_runes`/lane deltas) — see the crawler bullet above for the
extraction/backfill/endpoint details. Feeds the Overview "Recent games"
table's ▸/▾ "Curve" toggle (a two-line SVG chart per game: you vs the lane
opponent, gold + CS).
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
app's lazy-load convention. Research entries deliberately don't use this —
no video/clip attachments there, screenshots only (see below).
`game_reflections(match_id+puuid PK, tags, note, updated_at_ms)` — a
lightweight per-game tag/note from the tracked player's own perspective,
independent of matchup notes / block learnings / sessions: a fast post-match
reflection habit ("bad TP", "int death", "tilted", "good vision", ...)
rather than a full write-up. `tags` is a JSON array of freeform strings —
loose, no fixed vocabulary enforced server-side (the frontend just offers
quick-pick suggested chips plus a custom-tag input, same philosophy as
`research_entries`' champion fields); `note` is one Markdown field. Scoped
directly by `(match_id, puuid)` rather than `clips`' `owner_type`/`owner_id`,
so it works for ANY game a tracked player has played, not just block games.
`GET /api/reflections?match_id=&puuid=` returns `{tags, note}` (blank
defaults when nothing is recorded); `PUT /api/reflections/{match_id}/{puuid}`
is a partial update — only body keys present are written
(`db.set_reflection` mirrors `set_matchup_note`'s `_KEEP` sentinel), so a
tags-only edit (toggling a chip) never clobbers the note and vice versa. UI:
a small inline editor attached to per-game rows in two places — Overview's
"Recent games" table (`app.js`) and a block game's expanded stats panel
(`blocks.js`'s `gameMetricsPanel`) — sharing `reflectionSection`/
`wireReflectionSection` (app.js), the same shared-component pattern as
`clipsSection`/`wireClipsSection`. Tags render as toggleable chips
(`.chip-main` selected / `.chip-inactive` available); the note renders via
the shared `renderNotes()`/`md-body` convention with an edit/view toggle
like general champion notes.
`research_entries(id PK, player_name, champion, opp_champion, title, notes,
created_at_ms, updated_at_ms)` — a Research-tab study entry for someone
else's game; `champion`/`opp_champion` are optional freeform text (loosely
validated against the champion roster if given, like everywhere else, but
blank is fine — a research entry might be about macro/positioning, not a
specific matchup). `notes` is one Markdown field covering both general
observations and VOD notes together — no separate timestamp log; if the
user wants to note a moment in a VOD they just write it inline (e.g.
"14:32 — bad rotation") as part of the notes.
`research_screenshots(id PK, entry_id, caption, file_name, created_at_ms)`
— multiple screenshots per entry (no caption collected via the UI, though
the column stays optional for future use); files under
`<db_dir>/research-screenshots/<uuid><ext>` (15 MB cap, same image
extensions as the background-picture feature — `get_research_screenshots_dir()`
in app.py). API: `GET/POST/PATCH/DELETE /api/research[/{id}]`,
`POST /api/research/{id}/screenshots` + `GET .../file` + `DELETE
/api/research/screenshots/{id}`. Deleting an entry cascades screenshots
(`db.delete_research_entry`), unlinking their files in app.py first, same
division of responsibility as sessions/blocks. UI in `research.js`:
collapsed-by-default entry cards (list is lightweight — `GET /api/research`
returns only the entry rows; screenshots are fetched together with the
rest of the entry on first expand via `GET /api/research/{id}`).
`tier_lists(id PK, title, data, champion, created_at_ms, updated_at_ms)` — a
drag-and-drop champion tier board (Tier list tab, `static/tierlist.js`).
`data` is JSON `{tiers: [{label, color, image, image_kind, champions: [ids]}],
flagged: [ids]}` — a row icon is a champion or a rune name (`image_kind`),
`flagged` marks champions with a "?"; server-side `_validate_tier_data`
(app.py) cleans it (max `_MAX_TIERS`, hex colours, known champion ids, no
duplicate champion across tiers). `champion` splits the SAME table in two:
`''` = an editable list owned by the Tier list tab (`GET /api/tier-lists`,
`scope=all` also returns the rest), a champion = a read-only COPY saved into
that champion's Matchup guide. **Editing only happens in the tab** —
`POST /api/champions/{champion}/tier-lists` snapshots the open board into a
guide (several per champion; same title = overwrites that copy, reported as
`replaced`), `GET` lists them, and removal reuses `DELETE
/api/tier-lists/{id}`. The guide renders copies via `tierBoardStaticHtml()`
(tierlist.js, no drag/inputs) — `loadGuideTierLists` in guide.js — and the
tab's "⇄ Compare" overlay (`#tier-compare-overlay`) shows up to 4 of any
lists in a 2×2 grid using the same static renderer. `stats.champion_roles`
(`GET /api/champion-roles`) backs the pool's role filter.
`GET /api/export-all` — a full backup as one .zip (`api_export_all` in
app.py): `data.json` (sessions, blocks + block_games, matchup_notes,
champion_notes, champion_item_builds, research_entries +
research_screenshots, clips — JSON text columns decoded back to real JSON
for readability) plus every `kind='upload'` clip and screenshot file under
`clips/`/`screenshots/`. Deliberately excludes `settings` (API key,
accounts) and crawled match/rank data — Riot's API can always re-supply
the latter, and the former shouldn't leave the machine in a shareable
backup. Built to a temp file via `zipfile` (not in-memory — clips can be
up to 50 MB each) and streamed back via `FileResponse` with a
`BackgroundTask` cleanup once sent. Settings → Backup has a plain
`<a href download>` link (no JS needed, same pattern as the sessions
Markdown export). `POST /api/import-all/preview` + `POST /api/import-all`
restore one — scoped deliberately to a fresh/empty setup only (new
machine, reinstall), not merging into an already-used database: every row
keeps its original id/PK from the backup, and `_import_conflicts` checks
each one against what's already present *before* writing anything — if
even a single row would collide (matching id, PK, session date, etc.), the
whole import is refused with a 409 listing what conflicts, nothing
partially written. No id remapping is attempted; that's the tradeoff for
keeping this simple, matching the "fresh setup" scope the feature was
built for. Preview returns the same conflict list plus per-table counts,
which the UI uses for a `confirm()` before actually committing. Uploaded
as multipart (`UploadFile`, 500 MB cap — a backup can hold many clips),
re-uploaded a second time for the real import call rather than caching
the file server-side between preview and commit (stateless, simplest).

## Development rules

- **All UI work follows `STYLE.md`** — theme variables (never hardcode
  accent/surface colors), shared component patterns (buttons, editors,
  badges, collapsed add-forms), and the design-pass checklist live there.
- **All notes render as Markdown wherever they are displayed** — session
  notes, block learnings, block-game notes, matchup notes, champion notes,
  and any future note field. Use `renderNotes(...)` (vendored marked) inside
  an `md-body` element; never show raw/escaped note text in a read-only view.
- **Every user-facing feature adds an entry to `static/changelog.json`**
  (newest first; main functionality only, not tiny tweaks). It drives the 📋
  "What's new" panel; entries newer than the latest GitHub release tag show a
  "not yet released" badge.
- **Every `VERSION` bump gets a matching `vX.Y.Z` git tag, pushed right
  after the commit** (`git tag vX.Y.Z && git push origin vX.Y.Z`) — this is
  what actually publishes the build: `build.yml`'s `push: tags: ["v*"]`
  trigger runs the full build and its "Attach ... to release" steps only
  fire `if: startsWith(github.ref, 'refs/tags/')`, so a commit without a
  tag never reaches GitHub Releases (a `workflow_dispatch` run doesn't tag
  or release either — it's for manual ad-hoc testing builds only, artifacts
  downloaded straight from the run). Tags must be pushed from a real user
  credential (this Bash tool's `git push`, not a GITHUB_TOKEN-authored push
  from inside a workflow) — Actions suppresses further workflow triggers
  from its own token to prevent trigger loops, so a tag created *by* a
  workflow run would silently never kick off this one.

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
