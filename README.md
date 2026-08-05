# Coach Potato 🥔 — Coaching & Improvement App for League of Legends

A local app that crawls the Riot API match history for any number of accounts
into a SQLite database and turns it into a coaching toolkit: **matchup
winrates**, a **coaching-progress** tracker with per-session notes, **trends**
over time for every metric, deliberate-practice **blocks**, a per-champion
**matchup guide** (runes, item builds, skill orders, notes), and a **research
journal** for studying other players' games. Everything runs on your machine;
the only thing that leaves it is calls to Riot's API with your own key.

> Lane scope: statistics currently cover **top-lane games** (the lane the app
> was built around). The match data for all roles is already stored, so
> other-role support is a query/UI change, not a re-crawl.

## Desktop app (no Python required)

Packaged per-OS builds start the server, open a native window (or your
browser), and walk you through setup — API key, accounts, and server — in an
in-app **Settings** view (⚙). Data lives in your OS app-data directory, so it
survives upgrades.

- **Download** the latest build from the repo's
  [Releases](../../releases) page:
  - **Windows** — `CoachPotatoSetup.exe` (per-user installer, no admin/UAC) or
    `coach-potato-windows.exe` (portable single file).
  - **macOS** — `coach-potato-macos.zip`.
  - **Linux** — `coach-potato` (single file).
- **Build it yourself**: `pip install pyinstaller pywebview` then
  `pyinstaller --onefile --name coach-potato --add-data "static:static"
  --add-data "VERSION:." desktop.py`. The **Build desktop binaries** GitHub
  Actions workflow produces all three OS builds plus the Windows installer
  (runs on `v*` tags and manual dispatch).

The app checks for a newer release on startup and shows a "What's new" panel
(📋) so you can see what changed.

## Local setup (development)

Requirements: Python 3.11+ on Linux/macOS/WSL.

```bash
git clone <this repo> && cd coach-potato
./setup.sh              # creates .venv, installs deps, creates .env from .env.example
```

The **web app stores its settings in the database** (Settings view / `⚙`), so
for normal use you don't need `.env` at all — just run the server and fill in
the Settings page. `.env` is only a convenience fallback for development and
for the `crawl.py` CLI:

```ini
RIOT_API_KEY=RGAPI-...              # from https://developer.riotgames.com
ACCOUNTS=YourName#EUW, Smurf#EUW    # comma-separated Riot IDs, any number
PLATFORM=euw1                       # your server (na1, kr, eun1, ...); default euw1
```

Run it:

```bash
./crawl.sh --limit 5    # tiny test batch first — confirms the API key works
./crawl.sh              # full incremental crawl (see rate limits below)
./run.sh                # web UI at http://localhost:8321
```

On Windows without WSL, `run.ps1` is a PowerShell launcher equivalent to
`run.sh`.

Re-run `./crawl.sh` any time (or click **Update data** ⟳ in the UI) to pull
new games — the crawler is incremental and safe to interrupt/resume. The web
app also auto-updates on a configurable interval while it's open.

`PLATFORM` accepts any Riot platform id — `euw1 eun1 tr1 ru` (Europe),
`na1 br1 la1 la2` (Americas), `kr jp1` (Asia), `oc1 ph2 sg2 th2 tw2 vn2`
(SEA). The regional routing hosts for match history and account lookup are
derived from it automatically.

## Getting a Riot API key

Two kinds of key work, both free from <https://developer.riotgames.com>:

- **Personal API key (recommended).** Register a *personal product*
  ("Coach Potato — personal match-history tool" is a fine description;
  approval is lightweight). You get a **persistent key** that doesn't
  expire — set it once in the app's Settings and forget it.
- **Development key (quick start).** Available instantly on the portal
  front page, but **expires every 24 h**. When it does, crawls fail with a
  clear "API key expired" message — paste a fresh key in Settings
  (already-crawled data is unaffected; browsing the UI needs no key).

Keys are stored locally and only ever sent to Riot's API. Every user brings
their own key — the app ships without one by design.

Dev-key rate limits (20 req/s, 100 req/2 min) are respected automatically, so a
full first crawl of a large history takes roughly 2 minutes per ~100 matches.

## Overview & matchups

- **Matchup table** — per opponent champion: games, W–L, winrate bar (50 %
  reference tick), KDA, CS/min, gold/min, damage/min, average game length, plus
  optional coaching-metric averages via a column picker. Each row expands into
  an Overview (win/loss strip + block notes) and a per-game list; a 📖 link
  deep-links to that matchup's guide.
- **Grouped by opponent rank** — the same table bucketed by the lane
  opponent's current rank tier.
- **Summary tiles** — games, winrate, KDA, CS/min, current solo rank.
- **My champions** — performance per champion you played.
- **Recent games** — last 20 top-lane games with results, matchups, and an
  expandable side-by-side view of the runes both players actually ran.
- **Rank over time** — a chart from your tracked accounts' rank snapshots
  (coaching-session dates drawn as markers). Riot exposes no LP history, so
  density follows how often you crawl; between snapshots the line is
  interpolated from ranked win/loss and shown faint.
- **Filters** — period presets (7d/14d/30d/90d/180d/1y/all/custom dates), my
  champion, queue (Ranked Solo / Flex), opponent rank tier, min games, and a
  **blue/red side** filter, present across Overview, Matchups, Trends and
  Coaching progress.

## Coaching progress

The **Coaching progress** tab (or `#progress` in the URL) tracks improvement
between coaching sessions: a *Baseline* segment (30 days before your first
session), one segment per gap between sessions, and *Since last session* —
each with games, winrate, KDA, CS/min, gold/min, DMG/min and ▲/▼ deltas
against the previous segment, plus optional coaching-metric averages via a
column picker.

Each segment row expands (▸) into **detailed coaching metrics** pulled from
Riot's per-match `challenges` data, grouped as *Laning*, *Damage & fighting*,
*Objectives & map* and *Vision & survival*, each with a color-aware ▲/▼ delta
(less time dead = green). This includes **timeline lane deltas** measured
directly against your lane opponent — ΔCS, ΔLevel and ΔGold at ~7 and ~14
minutes — a truer "was I ahead" signal than Riot's binary laning flag. A nested
**Games (N)** expander lists the individual games.

Metrics for matches crawled before these features existed need a one-time,
resumable backfill:

```bash
./crawl.sh --backfill-metrics        # challenges-based metrics
./crawl.sh --backfill-lane-deltas    # timeline ΔCS/ΔLevel/ΔGold
./crawl.sh --backfill-runes          # actual runes played
./crawl.sh --backfill-jungle-sides   # jungle start halves (strong/weak side)
```

Each session has a **title** and full **notes in Markdown** — expand a session
(▸) to read the rendered notes, click *edit* to change title/notes, attach
short **clips** (uploaded video or a pasted YouTube/Twitch link) to specific
moments, and use **Export all (.md)** to download every session as one
Markdown document for sharing with your coach.

Progress stats combine **all tracked accounts** (coaching applies to you, not
the account), top lane only, remakes excluded. The champion filter defaults to
Gwen. A session's date boundary is midnight UTC.

## Blocks (deliberate practice)

The **Blocks** tab (`#blocks`) supports deliberate practice:

- A standing **champion pool** commitment — Main Blind, Core Pool, Counter
  Picks — edited in Settings (drag to set priority order) and shown read-only
  on the Blocks page.
- **Configurable-size blocks** (default 3 games): add a game and it joins the
  current block; once the block is full the next game opens a new one. Champion,
  matchup and result are auto-filled from the crawled match — you write the
  per-game notes and the block's Markdown **learnings** summary. Blocks can
  **auto-close on a time gap** (default 3 h between games) so a fresh session
  starts a fresh block.
- **Block series** group blocks into a named run (e.g. a two-week challenge)
  and number them from #1; can be turned off for plain continuous numbering.
- Add games from the picker under the block list, or promote any row from the
  Overview's *Recent games* / a coaching segment's game list with its **+
  Block** button. A game can be in only one block. Each block game's expanded
  panel shows its full metrics, the runes both players ran, and its own clips.

The web app deepens block-game timeline data in the background so the lane
deltas fill in shortly after a block is opened (⏳ marks games still fetching).

## Matchup guide

The **Matchup guide** tab is a per-champion playbook. Pick "My champion" from
the full roster and get:

- **General champion notes** (Markdown) and a mobafire-style **item build** —
  an ordered core build plus labeled situational sections, picked from
  current-patch item icons.
- **Per-matchup rune pages** (one or several per opponent, built with a full
  click-through picker), a freeform **patch** tag, and Markdown **notes** — for
  every matchup you've faced, or add one for a matchup you haven't played yet.
  A "General runes" mode instead keeps a single champion-level rune set next to
  the item build (switchable in Settings; per-matchup pages are preserved).
- **Skill order** per matchup, saved from the cooldown tool and shown
  read-only.
- **Recent games** per matchup with the runes you actually ran.
- **📄 One Pager** — a full-screen, second-screen quick reference (runes, skill
  order, item build, notes) with no stats to distract.
- **⏱ Cooldown comparison** — an ability-haste-aware cooldown calculator with a
  skill-order grid and an "at level" / "level matrix" view.
- **📡 Live Lookup** — finds your live game (spectator API), switches to the
  champion you're playing, and opens the One Pager for the likely matchup (Riot
  exposes no lane live, so the opponent is guessed from history with a quick
  switcher to correct it).
- **Export / Import** — export one champion's whole guide as JSON (optionally
  password-encrypted with a real cipher) and import it elsewhere with an
  overwrite preview, or export a printable **PDF** of the guide.

## Player comparison (opt-in)

Enable **Research & comparison** in Settings and add other players by Riot ID
to compare yourself against in a matchup. The ⧉ button on a Matchup-guide row
opens a side-by-side view of their stats, runes and recent games in that
matchup — useful for theorycrafting runes and notes. Each player toggles on/off;
their games are fetched on demand (last 60 days by default, with "Fetch more").
Their data never mixes into your own tracked stats.

## Research journal

The **Research** tab is a study journal for *other* players' games, unrelated
to the tracked-account crawler: freeform entries with a player name, optional
champion/opponent, one Markdown notes field (general observations and VOD notes
together — write timestamps inline), and multiple screenshots. No video/clip
attachments here — screenshots only.

## Trends

The **Trends** tab (`#trends`) tracks every stat over time: small line charts
for each Core stat and every coaching metric, grouped like the coaching view,
plus a **breakdown table** of all values per period. Bucket by month (default),
week (Monday-start), or day; filter by champion, queue and side. All accounts
combined, top lane only.

## Appearance & data

- **Appearance** (Settings) — UI opacity, an uploaded background picture, a
  custom accent color, a date format (ISO / US / EU), and a "hide my rank/LP"
  toggle that redacts your own rank everywhere.
- **Backup** — *Export everything* downloads one `.zip` (sessions, blocks,
  matchup/champion guides, item builds, research entries, and all uploaded
  clips/screenshots). *Import from a backup* restores it on a fresh install
  (scoped to an empty setup; it refuses rather than merge into an in-use db).
  Deliberately excludes settings (API key/accounts) and crawled match data —
  Riot's API can re-supply the latter.

## Design decisions & known limitations

1. **"Group by rank" = lane opponent's *current* solo-queue rank.** The Riot
   API stores no historical rank, so a Gold opponent who has since climbed to
   Plat counts as Plat. Ranks are cached 7 days.
2. **Queues crawled: 420 (Ranked Solo) + 440 (Ranked Flex)** by default.
   Add more: `./crawl.sh --queues 420 440 400 490`.
3. **Remakes (< 5 min) are excluded** from all statistics.
4. **Matchup = enemy participant with teamPosition TOP** in games where the
   tracked player is TOP; games where Riot's position data has no enemy TOP
   appear in summary totals but not the matchup table.

## Architecture

```
crawl.py            CLI crawler (also triggered from the UI)
desktop.py          PyInstaller entry point (native window via pywebview)
server/
  config.py         settings resolution (db table + .env fallback), paths
  riot_client.py    Riot HTTP client + sliding-window rate limiter
  parsing.py        match-v5 JSON -> db rows
  crawler.py        incremental crawl (watermarks), rank/metric/rune/timeline enrichment
  metrics.py        single-source coaching-metric registry (DDL, parse, SQL, meta)
  rune_data.py      rune tree/rune/shard roster + match-v5 perks decoding
  crypto.py         guide export/import encryption (PBKDF2 + Fernet)
  pdf_export.py     matchup-guide PDF (reportlab)
  db.py             sqlite schema + helpers (data/lol.sqlite, WAL)
  stats.py          SQL aggregation: matchups, summaries, progress, trends, filters
  app.py            FastAPI JSON API + serves static/
static/             vanilla HTML/JS/CSS frontend (no build step)
tests/              pytest suite (offline, no API key needed)
```

### API examples

```bash
curl 'localhost:8321/api/players'
curl 'localhost:8321/api/stats/matchups?puuid=<PUUID>&range=30d&min_games=2&side=blue'
curl 'localhost:8321/api/stats/matchups_by_rank?puuid=<PUUID>&champion=Kled'
curl 'localhost:8321/api/stats/summary?puuid=<PUUID>&from=2026-01-01&to=2026-06-30'
curl 'localhost:8321/api/stats/progress?champion=Gwen'
curl 'localhost:8321/api/sessions/export.md'
```

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # run tests (no network or key needed)
```

- **All UI work follows `STYLE.md`** (theme variables, shared components, the
  design-pass checklist).
- See `CLAUDE.md` for architecture notes, schema, and gotchas, and
  `docs/superpowers/` for design specs and implementation plans.
```
