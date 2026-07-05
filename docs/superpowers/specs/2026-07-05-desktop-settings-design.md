# In-App Settings + Desktop Build + Blocks UI polish — Design

**Date:** 2026-07-05
**Status:** Approved by user ("go ahead and do that and make a build").

## 1. In-app settings (replaces .env at runtime)

- New `settings(key TEXT PK, value TEXT)` table; keys `riot_api_key`,
  `accounts` (JSON list of "Name#TAG"), `platform`.
- `config.default_db_path()`: `LOL_DB_PATH` env → that; frozen (PyInstaller)
  → OS app-data dir (`%APPDATA%\CoachPotato`, `~/Library/Application
  Support/CoachPotato`, `$XDG_DATA_HOME/coach-potato`); else
  `PROJECT_ROOT/data/lol.sqlite`.
- `config.resolve_settings(conn) -> {riot_api_key, accounts, platform,
  configured, source}`: db values win; missing values fall back to parsing
  `.env` under `config.ENV_FALLBACK_ROOT` (defaults to PROJECT_ROOT; frozen
  bundles naturally have none; tests monkeypatch it). Nothing is silently
  persisted — saving via the API is the only write.
- API: `GET /api/settings` (effective values + `configured`, `source`,
  `platforms` list); `PUT /api/settings` validates (key non-empty, accounts
  `Name#TAG`, platform in PLATFORM_ROUTING) → persists to db.
- `_run_crawl` + `get_db_path` stop using `load_config`; CLI `crawl.py`
  keeps `.env` (dev workflow unchanged).
- Frontend: ⚙ Settings view (nav tab, `#settings`): API key input (with
  24 h dev-key note + developer.riotgames.com link), accounts chip box
  (Name#TAG), platform select, Save. First run (`configured: false`) lands
  on Settings with a welcome banner. Empty-players state no longer replaces
  the whole page — tabs stay usable so Settings/Update data are reachable.

## 2. Desktop build

- `desktop.py`: picks a free port (prefer 8321), runs uvicorn in a thread,
  then opens a native window via `pywebview` when importable/usable, else
  the system browser (process stays alive until Ctrl+C / window close).
- PyInstaller one-file build: `pyinstaller --onefile --name coach-potato
  --add-data static:static desktop.py` (paths resolve because
  `PROJECT_ROOT` lands on `_MEIPASS` when frozen). Local Linux build for
  the user to test; `.github/workflows/build.yml` matrix
  (ubuntu/windows/macos, workflow_dispatch + tags) uploads per-OS binaries.
- Packaged first run: fresh app-data db → Settings onboarding.

## 3. Blocks UI

- **Collapsible blocks**: ▸/▾ toggle in the card head; collapsed shows only
  the head row (number, badge, count/W–L, title). Default expanded;
  collapsed ids persisted in `localStorage` (`cp-collapsed-blocks`) so state
  survives restarts.
- **Per-game stats**: ▸ toggle per game row expands the same metric groups
  shown in Coaching progress but with that single game's values, no deltas.
  Backend: `stats.single_game_metrics(conn, match_id, puuid)` transforms the
  raw `participant_metrics` row per agg kind (avg → raw, pct01 → ×100,
  per_min → 60·v/duration, pct_time → 100·v/duration); served by
  `GET /api/stats/games/metrics?match_id&puuid` (404 when no metrics row),
  response includes `meta`. Client caches per entry.

## Testing

TDD on settings storage/resolution/validation, default_db_path modes,
single_game_metrics math + endpoint. Binary smoke test: run built
executable against a temp db, exercise `/api/settings` PUT/GET and static
serving. Screenshots: settings view, collapsed blocks, expanded game stats.

## Out of scope

Auto-update, code signing, tray icon, Riot production key application,
Windows/macOS local builds (CI workflow provided instead).
