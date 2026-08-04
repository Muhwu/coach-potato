# Strong Side / Weak Side Auto-Detection — Design

**Date:** 2026-08-04
**Status:** Proposed (not implemented)

## Purpose

PR #20 added a *manual* Strongside/Weakside flag per block game, so a lane
deficit can be read in context rather than as a failure. This proposes
detecting that flag automatically from the match timeline, for both the
tracked player and their lane counterpart on the enemy team.

## The rule

The user's definition: **you are strong side when your jungler starts on the
map half opposite your lane.**

Both halves are *absolute*, not team-relative. Summoner's Rift splits along the
mid-lane diagonal into a **top half** (top lane, both teams' top-side jungles)
and a **bot half**. Top lane is in the top half for blue and red alike, so:

```
strongside(player) = jungle_start_half(player's team) != lane_half(player)
```

`lane_half` is `top` for TOP, `bot` for BOTTOM/UTILITY, undefined for
MIDDLE/JUNGLE.

Worked example (from the user): red team, top lane, jungler starts their blue
buff. Red's blue buff sits in the bot half; lane half is top; they differ →
**strong side**.

Because the lane counterpart shares the same `lane_half`, their verdict is the
same comparison against the *enemy* jungler's start half. That yields four
combinations per game — both strong, both weak, or split — which is more
informative than a single flag.

## Signal: jungler position in the early timeline

Regular jungle camps don't emit timeline events (only `ELITE_MONSTER_KILL`, for
dragon/herald/baron), so the practical signal is **participant position**.
Match-v5 `participantFrames` carry `position: {x, y}` alongside the
`level`/`totalGold` fields `metrics.parse_timeline_deltas` already reads.

> Verify this field against one real timeline payload before building on it —
> it is the only external assumption here, and nothing in the codebase reads
> `position` today (the test fixture in `tests/test_metrics.py` emits only
> CS/level/gold, so it needs extending).

Geometry:

```
half(x, y) = "top" if y > x else "bot"     # split along the mid diagonal
```

Approximate buff coordinates as a sanity check: blue team's blue buff
≈ (3800, 7900) → top; red team's blue buff ≈ (11000, 6900) → bot, matching the
worked example above. Confirm against real data rather than trusting these
figures.

**Which frame:** not frame 0 — everyone is in the fountain, and blue's fountain
sits on the diagonal where the test is meaningless. Use the earliest frame
where the jungler's `jungleMinionsKilled >= 1` (first camp cleared), falling
back to the 60s frame. More robust than a fixed minute against slow starts and
leashes.

## Storage: record the fact, not the verdict

Store *where each jungler started*, not who was strong side, so a later change
to the definition never requires a re-crawl.

Two additive nullable columns on `matches`, via the existing `PRAGMA
table_info` guard in `db._migrate`:

```
jungle_start_100 TEXT   -- 'top' | 'bot' | NULL (unknown)
jungle_start_200 TEXT
```

`matches` is one row per match and these are match-level facts, so no new table
is needed. (`match_jungle_starts` is the alternative if confidence and raw
coordinates should also be stored.)

The per-player verdict is derived in `stats._BASE` and exposed as
`my_strongside` / `opp_strongside` next to the existing lane deltas — which
lands it in matchups, block games and progress segments for free.

## Capture and backfill

- `metrics.parse_jungle_starts(timeline)` → `{100: "top"|"bot"|None, 200: ...}`,
  beside `parse_timeline_deltas`.
- The crawler **already fetches the timeline** for every new match
  (`Crawler._safe_timeline`) and discards everything but the deltas, so capture
  costs no extra API calls.
- Extend `backfill_lane_deltas` to fill both from its single timeline fetch.
- A dedicated `backfill_jungle_sides()` / `./crawl.sh --backfill-jungle-sides`
  is then only needed for matches already marked `has_timeline=1` from before
  this feature. Same rate-limited pattern as the other backfills (~2 min per
  100 matches); reuse the block-games-first prioritisation the web app already
  does for timelines.

## How it surfaces

The manual `weakside` column from PR #20 stays as the **override**; detection
fills the default:

- The Blocks select becomes **Auto (Strongside) / Strongside / Weakside**,
  mirroring the Lane result picker's Auto behaviour that PR established. No
  data migration for anyone who already set the flag by hand.
- The per-game panel shows both sides: *"You: strong · Enemy top: weak (their
  jungler started top)"* — the context manual entry can't provide.
- A **Side** column in the block games table, distinguishing detected from
  manually set.
- Later, the real coaching payoff: **split lane deltas by side**. "ΔCS@14 is +8
  strong side, −12 weak side" is a sharper signal than the aggregate, and cheap
  once the column exists.

Naming trap: `?side=` is already the blue/red team filter in `stat_filters`. A
filter for this needs a different name — `?lane_side=` or `?jungle_side=`.

## Limitations (state these in the UI where relevant)

- **It measures the start, not the attention.** A jungler who starts opposite
  and never comes near you still counts as strong side. That is what the
  definition specifies; a proximity- or first-gank-based measure is a separate,
  much fuzzier feature.
- **Invades.** A jungler starting in the enemy jungle registers as the half
  they are physically in. Preferring the first cleared-camp frame mostly avoids
  this; the remainder is rare enough to accept, optionally marked low
  confidence.
- **No jungler.** Riot's `team_position` is occasionally empty or doubles a
  role → NULL, and the UI shows "—" rather than guessing.
- **Mid lane** has no strong/weak side under this definition → NULL, not a
  forced value.
- **Bot lane** ADC and support share one value.

## Testing and validation

- Synthetic timelines with known positions covering all four team × half
  combinations, asserting both the tracked player and the lane counterpart.
- A table-driven test for the geometry helper, including the on-diagonal
  fountain case.
- After backfilling a few hundred games: check the strong/weak split is near
  50/50 (a large skew means the diagonal test or the frame choice is wrong),
  and hand-verify a handful against replays or op.gg.

## Sequencing

1. Parser + geometry helper + tests — no schema change, verifiable in isolation
   against a real timeline payload.
2. Schema columns, crawler capture, backfill.
3. `stats` exposure + the Blocks Auto option.
4. Optional: the side filter and the strong-vs-weak metric split.

Steps 1–2 carry the risk (the `position` field and the coordinate convention);
3–4 are mechanical once the data is there.
