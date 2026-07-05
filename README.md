# Coach Potato 🥔 — LoL Top Lane Stats

A small local web app that crawls the Riot API match history for any number of
League of Legends accounts into a SQLite database and shows **top-lane matchup
winrates** (plus KDA, CS/min, gold/min, damage/min), filterable by date range,
own champion, queue, and opponent rank — plus a **coaching progress** view that
tracks your improvement between coaching sessions.

## Desktop app (no Python required)

A packaged single-file build is available: it starts the server, opens a
native window (or your browser), and walks you through setup — API key,
accounts, and server — in an in-app **Settings** view (⚙). Data lives in
your OS app-data directory.

Build it yourself with `pip install pyinstaller pywebview` then
`pyinstaller --onefile --name coach-potato --add-data "static:static"
desktop.py`, or download per-OS binaries from the **Build desktop binaries**
GitHub Actions workflow (runs on tags and manual dispatch).

## Local setup (development)

Requirements: Python 3.11+ on Linux/macOS/WSL.

```bash
git clone <this repo> && cd coach-potato
./setup.sh              # creates .venv, installs deps, creates .env from .env.example
```

Then edit `.env`:

```ini
RIOT_API_KEY=RGAPI-...              # from https://developer.riotgames.com
ACCOUNTS=YourName#EUW, Smurf#EUW    # comma-separated Riot IDs, any number
PLATFORM=euw1                       # your server (na1, kr, eun1, ...); default euw1
```

And run:

```bash
./crawl.sh --limit 5    # tiny test batch first — confirms the API key works
./crawl.sh              # full incremental crawl (see rate limits below)
./run.sh                # web UI at http://localhost:8321
```

Re-run `./crawl.sh` any time (or click **Update data** in the UI) to pull new
games — the crawler is incremental and safe to interrupt/resume.

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

## What you get

- **Matchup table** — per opponent champion: games, W–L, winrate bar (50 %
  reference tick), KDA, CS/min, gold/min, damage/min, average game length.
- **Grouped by opponent rank** — same table bucketed by rank tier.
- **Summary tiles** — games, winrate, KDA, CS/min, current solo rank.
- **My champions** — performance per champion you played.
- **Recent games** — last 20 top-lane games with results and matchups.
- **Filters** — period presets (7d/14d/30d/90d/180d/1y/all/custom dates),
  my champion, queue (Ranked Solo / Flex), opponent rank tier, min games.

## Coaching progress view

The **Coaching progress** tab (or `#progress` in the URL) tracks improvement
between coaching sessions: a *Baseline* segment (30 days before your first
session), one segment per gap between sessions, and *Since last session* —
each with games, winrate, KDA, CS/min, gold/min, DMG/min and ▲/▼ deltas
against the previous segment. Add sessions (date + optional title) and delete
them in the same view.

Each segment row expands (▸) into **~22 detailed coaching metrics** pulled
from Riot's per-match `challenges` data, grouped as *Laning* (CS@10, lane
advantage @7/@14 min, max CS/level leads, plates, solo kills), *Damage &
fighting* (team damage share, kill participation, skillshots dodged...),
*Objectives & map* and *Vision & survival* — each with the same ▲/▼ delta vs
the previous segment (color-aware: less time dead = green). A nested
**Games (N)** expander lists the individual games — date, account, champion,
lane opponent and their rank, result, K/D/A, CS/min, game length.

Metrics for matches crawled before this feature exist need a one-time
backfill: `./crawl.sh --backfill-metrics` (~2 min per 100 matches).

Each session has a **title** and full **notes in Markdown** — expand a session
(▸) to read the rendered notes, click *edit* to change title/notes, and use
**Export all (.md)** to download every session as one Markdown document
(newest first) for sharing with your coach.

Notes:
- Progress stats combine **all tracked accounts** (coaching applies to you,
  not the account), top lane only, remakes excluded.
- The champion filter defaults to **Gwen**; queue filter available.
- A session's date boundary is midnight UTC — games played on the session day
  count toward the *after* segment.

## Block Learnings

The **Blocks** tab (`#blocks`) supports deliberate practice:

- A standing **champion pool** commitment at the top — Main Blind, Core Pool,
  Counter Picks — editable any time.
- **Three-game blocks**: add a game and it joins the current block; the 4th
  game automatically opens the next block. Each game shows champion, matchup
  and result auto-filled from the crawled match — you only write the per-game
  notes. Each block carries a title and Markdown **learnings** summary.
- Add games from the picker under the block list, or promote any row from
  the Overview's *Recent games* / a coaching segment's game list with its
  **+ Block** button. A game can be in only one block.

## Trends view

The **Trends** tab (`#trends`) tracks every stat over time: small
line charts for each Core stat (games, winrate, KDA, CS/min, gold/min,
DMG/min) and every coaching metric, grouped like the coaching view, plus a
**breakdown table** of all values per period at the bottom. Bucket by
**month** (default), week (Monday-start), or day; filter by champion and
queue. Both accounts combined, top lane only.

## Design decisions & known limitations

1. **"Group by rank" = lane opponent's *current* solo-queue rank.** The Riot
   API stores no historical rank, so a Gold opponent who has since climbed to
   Plat counts as Plat. Ranks are cached 7 days (`player_ranks` table). The
   schema stores all participants, so switching to op.gg-style "average lobby
   rank" is possible later (needs ~10× more rank lookups).
2. **Queues crawled: 420 (Ranked Solo) + 440 (Ranked Flex)** by default.
   Add more: `./crawl.sh --queues 420 440 400 490`.
3. **Remakes (< 5 min) are excluded** from all statistics.
4. **Matchup = enemy participant with teamPosition TOP** in games where the
   tracked player is TOP; games where Riot's position data has no enemy TOP
   appear in summary totals but not the matchup table.

## Architecture

```
crawl.py            CLI crawler (also triggered from the UI)
server/
  config.py         .env loading, accounts, paths
  riot_client.py    Riot HTTP client + sliding-window rate limiter
  parsing.py        match-v5 JSON -> db rows
  crawler.py        incremental crawl (watermarks), rank enrichment
  db.py             sqlite schema + helpers (data/lol.sqlite, WAL)
  stats.py          SQL aggregation: matchups, summaries, progress, filters
  app.py            FastAPI JSON API + serves static/
static/             vanilla HTML/JS/CSS frontend (no build step)
tests/              pytest suite (offline, no API key needed)
```

### API examples

```bash
curl 'localhost:8321/api/players'
curl 'localhost:8321/api/stats/matchups?puuid=<PUUID>&range=30d&min_games=2'
curl 'localhost:8321/api/stats/matchups_by_rank?puuid=<PUUID>&champion=Kled'
curl 'localhost:8321/api/stats/summary?puuid=<PUUID>&from=2026-01-01&to=2026-06-30'
curl 'localhost:8321/api/stats/progress?champion=Gwen'
curl 'localhost:8321/api/sessions/export.md'
```

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # run tests (no network or key needed)
```

See `CLAUDE.md` for architecture notes and gotchas, and
`docs/superpowers/` for design specs and implementation plans.
