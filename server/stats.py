"""Aggregated matchup statistics.

All functions operate on the tracked player's games in any role, excluding
remakes (< 300 s). The lane opponent is the enemy participant in the same
role (team_position); rank buckets use the opponent's current solo rank
('UNKNOWN' when never fetched / unranked).
"""
import json
import time
from datetime import datetime, timezone

from .metrics import METRICS, metric_keys, strongside

REMAKE_S = 300

# Absolute ladder points: 400 per tier (100 per division), apex tiers share a
# base and are separated by raw LP.
_TIER_BASE = {"IRON": 0, "BRONZE": 400, "SILVER": 800, "GOLD": 1200,
              "PLATINUM": 1600, "EMERALD": 2000, "DIAMOND": 2400,
              "MASTER": 2800, "GRANDMASTER": 2800, "CHALLENGER": 2800}
_DIVISION_OFFSET = {"IV": 0, "III": 100, "II": 200, "I": 300}


def rank_value(tier, division, lp):
    """(tier, division, lp) -> absolute ladder points, None when unranked."""
    if tier not in _TIER_BASE:
        return None
    base = _TIER_BASE[tier]
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return base + (lp or 0)
    return base + _DIVISION_OFFSET.get(division, 0) + (lp or 0)


LP_PER_GAME = 20  # crude estimate: solo-queue gain/loss per ranked game

_TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
_DIVISION_ORDER = ["IV", "III", "II", "I"]


def value_to_rank(value):
    """Inverse of rank_value, for estimated points (apex collapses to MASTER)."""
    value = max(0, int(value))
    if value >= 2800:
        return ("MASTER", None, value - 2800)
    return (_TIER_ORDER[value // 400], _DIVISION_ORDER[(value % 400) // 100], value % 100)


def _lp_delta(win):
    return LP_PER_GAME if win else -LP_PER_GAME


def _with_estimates(real, games):
    """Interleave ±LP_PER_GAME estimates from ranked-solo results around the
    real snapshots: backward from the first anchor (reconstructing history
    that predates snapshotting), forward from each one. Each real snapshot
    resets the accumulated drift."""
    if not real:
        return real

    def est(t, value):
        value = max(0, value)
        tier, division, lp = value_to_rank(value)
        return {"t": t, "tier": tier, "division": division, "lp": lp,
                "value": value, "estimated": True}

    points = []
    back, value = [], real[0]["value"]
    for g in reversed([g for g in games if g["t"] < real[0]["t"]]):
        back.append(est(g["t"], value))  # value just after this game
        value -= _lp_delta(g["win"])
    points.extend(reversed(back))
    for i, anchor in enumerate(real):
        points.append({**anchor, "estimated": False})
        end = real[i + 1]["t"] if i + 1 < len(real) else float("inf")
        value = anchor["value"]
        for g in (g for g in games if anchor["t"] < g["t"] < end):
            value += _lp_delta(g["win"])
            points.append(est(g["t"], value))
    return points


def rank_history(conn, puuids):
    """Chronological rank points per puuid: {puuid: [{t, tier, division, lp,
    value, estimated}]}. Real snapshots come from rank_history (unranked ones
    skipped); between them, ranked-solo win/loss results add ±LP_PER_GAME
    estimated points."""
    series = {p: [] for p in puuids}
    if not puuids:
        return series
    slots = ", ".join("?" for _ in puuids)
    rows = conn.execute(
        f"""SELECT puuid, solo_tier, solo_division, solo_lp, fetched_at_ms
            FROM rank_history WHERE puuid IN ({slots}) AND solo_tier IS NOT NULL
            ORDER BY fetched_at_ms""", list(puuids))
    for r in rows:
        series[r["puuid"]].append({
            "t": r["fetched_at_ms"], "tier": r["solo_tier"],
            "division": r["solo_division"], "lp": r["solo_lp"],
            "value": rank_value(r["solo_tier"], r["solo_division"], r["solo_lp"]),
        })
    for puuid in puuids:
        games = [dict(g) for g in conn.execute(
            """SELECT m.game_creation_ms AS t, pa.win FROM participants pa
               JOIN matches m ON m.match_id = pa.match_id
               WHERE pa.puuid=? AND m.queue_id=420 AND m.game_duration_s >= ?
               ORDER BY m.game_creation_ms""", (puuid, REMAKE_S))]
        series[puuid] = _with_estimates(series[puuid], games)
    return series

_METRIC_SELECT = ",\n       ".join(f"pm.{k} AS {k}" for k in metric_keys())

# One row per (my TOP game, enemy TOP opponent). LEFT JOIN keeps games
# where the enemy team has no TOP (position data missing) for summary().
_BASE = """
SELECT m.match_id, m.game_creation_ms, m.game_duration_s, m.queue_id,
       me.puuid AS my_puuid,
       me.champion_name AS my_champion, me.win, me.kills, me.deaths, me.assists,
       me.cs, me.gold_earned, me.damage_to_champions,
       me.summoner1_id AS spell1, me.summoner2_id AS spell2, me.items AS my_items_json,
       me.starting_items AS my_starting_items_json, me.build_order AS my_build_order_json,
       me.skill_order AS my_skill_order_json,
       opp.champion_name AS opp_champion, opp.puuid AS opp_puuid,
       COALESCE(pr.solo_tier, 'UNKNOWN') AS rank_tier,
       pm.match_id AS pm_match_id,
       myr.runes AS my_runes_json,
       oppr.runes AS opp_runes_json,
       """ + _METRIC_SELECT + """
FROM participants me
JOIN matches m ON m.match_id = me.match_id
LEFT JOIN participants opp ON opp.match_id = me.match_id
    AND opp.team_id != me.team_id AND opp.team_position = me.team_position
LEFT JOIN player_ranks pr ON pr.puuid = opp.puuid
LEFT JOIN participant_metrics pm
    ON pm.match_id = me.match_id AND pm.puuid = me.puuid
LEFT JOIN participant_runes myr
    ON myr.match_id = me.match_id AND myr.puuid = me.puuid
LEFT JOIN participant_runes oppr
    ON oppr.match_id = me.match_id AND oppr.puuid = opp.puuid
WHERE me.puuid IN ({puuid_slots}) AND me.team_position != ''
  AND m.game_duration_s >= :remake_s
"""


def _metric_agg_select():
    exprs = []
    for m in METRICS:
        k = m["key"]
        if m["agg"] == "avg":
            e = f"AVG({k})"
        elif m["agg"] == "pct01":
            e = f"100.0 * AVG({k})"
        elif m["agg"] == "per_min":
            e = f"60.0 * SUM({k}) / SUM(CASE WHEN {k} IS NOT NULL THEN game_duration_s END)"
        elif m["agg"] == "pct_time":
            e = f"100.0 * SUM({k}) / SUM(CASE WHEN {k} IS NOT NULL THEN game_duration_s END)"
        else:  # pragma: no cover — registry is validated by tests
            raise ValueError(m["agg"])
        exprs.append(f"{e} AS {k}")
    return ",\n".join(exprs)


# which map side I was on: blue = team 100, red = team 200
_SIDE_TEAM = {"blue": 100, "red": 200}


def _filtered_base(puuid, from_ms=None, to_ms=None, champion=None, queues=None,
                   rank_tier=None, require_opponent=True, opp_champion=None, side=None,
                   roles=None):
    puuids = [puuid] if isinstance(puuid, str) else list(puuid)
    sql = _BASE.format(puuid_slots=",".join(f":puuid{i}" for i in range(len(puuids))))
    params = {"remake_s": REMAKE_S}
    params.update({f"puuid{i}": p for i, p in enumerate(puuids)})
    if side in _SIDE_TEAM:
        sql += " AND me.team_id = :side_team"
        params["side_team"] = _SIDE_TEAM[side]
    # roles: restrict to the tracked player's team_position(s) — the role filter.
    # None/empty = all roles (the lane opponent is still the enemy in my role).
    roles = [r for r in (roles or []) if r]
    if roles:
        slots = ",".join(f":role{i}" for i in range(len(roles)))
        sql += f" AND me.team_position IN ({slots})"
        params.update({f"role{i}": r for i, r in enumerate(roles)})
    if opp_champion:
        sql += " AND opp.champion_name = :opp_champion"
        params["opp_champion"] = opp_champion
    if require_opponent:
        sql += " AND opp.puuid IS NOT NULL"
    if from_ms is not None:
        sql += " AND m.game_creation_ms >= :from_ms"
        params["from_ms"] = from_ms
    if to_ms is not None:
        sql += " AND m.game_creation_ms <= :to_ms"
        params["to_ms"] = to_ms
    if champion:
        sql += " AND me.champion_name = :champion"
        params["champion"] = champion
    if queues:
        placeholders = ",".join(f":q{i}" for i in range(len(queues)))
        sql += f" AND m.queue_id IN ({placeholders})"
        params.update({f"q{i}": q for i, q in enumerate(queues)})
    if rank_tier:
        sql += " AND COALESCE(pr.solo_tier, 'UNKNOWN') = :rank_tier"
        params["rank_tier"] = rank_tier
    return sql, params


_AGG = """
COUNT(*) AS games,
SUM(win) AS wins,
AVG(CAST(win AS REAL)) AS winrate,
AVG(kills) AS kills,
AVG(deaths) AS deaths,
AVG(assists) AS assists,
(SUM(kills) + SUM(assists)) * 1.0 / MAX(SUM(deaths), 1) AS kda,
SUM(cs) * 60.0 / SUM(game_duration_s) AS cs_min,
SUM(gold_earned) * 60.0 / SUM(game_duration_s) AS gold_min,
SUM(damage_to_champions) * 60.0 / SUM(game_duration_s) AS dmg_min,
AVG(game_duration_s) AS avg_duration_s
"""


def _pack_metrics(row):
    """Split a query row into base fields + a nested `metrics` dict (per-metric
    averages), matching the shape the frontend metric columns/panels expect."""
    record = dict(row)
    record["metrics"] = {k: record.pop(k) for k in metric_keys()}
    return record


def matchups(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
             rank_tier=None, min_games=1, side=None, roles=None):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier,
                                  side=side, roles=roles)
    params["min_games"] = min_games
    sql = f"""
        SELECT opp_champion, {_AGG},
               {_metric_agg_select()}
        FROM ({base})
        GROUP BY opp_champion
        HAVING COUNT(*) >= :min_games
        ORDER BY games DESC, winrate DESC
    """
    return [_pack_metrics(r) for r in conn.execute(sql, params)]


def matchups_by_rank(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
                     rank_tier=None, min_games=1, side=None, roles=None):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier,
                                  side=side, roles=roles)
    params["min_games"] = min_games
    sql = f"""
        SELECT rank_tier, opp_champion, {_AGG},
               {_metric_agg_select()}
        FROM ({base})
        GROUP BY rank_tier, opp_champion
        HAVING COUNT(*) >= :min_games
        ORDER BY rank_tier, games DESC
    """
    return [_pack_metrics(r) for r in conn.execute(sql, params)]


def summary(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
            rank_tier=None, min_games=1, side=None, roles=None):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier,
                                  require_opponent=False, side=side, roles=roles)
    totals = conn.execute(
        f"SELECT {_AGG} FROM ({base})", params
    ).fetchone()
    by_champion = [
        dict(r) for r in conn.execute(
            f"""SELECT my_champion AS champion, {_AGG}
                FROM ({base}) GROUP BY my_champion
                HAVING COUNT(*) >= :min_games
                ORDER BY games DESC""",
            {**params, "min_games": min_games},
        )
    ]
    recent = [_decode_game_runes(r) for r in conn.execute(
        f"""SELECT match_id, game_creation_ms, game_duration_s, queue_id,
                   my_puuid, my_champion, opp_champion, opp_puuid, rank_tier, win,
                   kills, deaths, assists, cs, my_runes_json, opp_runes_json
            FROM ({base}) ORDER BY game_creation_ms DESC LIMIT 20""",
        params)]
    result = dict(totals) if totals["games"] else {"games": 0, "wins": 0, "winrate": None}
    result["by_champion"] = by_champion
    result["recent"] = recent
    return result


def _rune_breakdown(conn, base, params, field):
    """Games + win rate grouped by a rune-page field (keystone/secondary_tree)
    extracted from the runes actually played. Skips games with no recorded
    runes or a blank field."""
    sql = f"""
        SELECT json_extract(my_runes_json, '$.{field}') AS name,
               COUNT(*) AS games, SUM(win) AS wins, AVG(CAST(win AS REAL)) AS winrate
        FROM ({base})
        WHERE my_runes_json IS NOT NULL AND my_runes_json != ''
          AND json_extract(my_runes_json, '$.{field}') IS NOT NULL
          AND json_extract(my_runes_json, '$.{field}') != ''
        GROUP BY name
        ORDER BY games DESC, winrate DESC
    """
    return [dict(r) for r in conn.execute(sql, params)]


def rune_analysis(conn, puuid, champion, opp_champion=None, queues=None):
    """Win rate by keystone and by secondary tree, from the runes actually
    played (participant_runes) in your games on `champion` — narrowed to vs
    `opp_champion` when given. Feeds the Matchup guide's rune analysis: "which
    keystone/secondary has the best win rate for me here"."""
    base, params = _filtered_base(puuid, champion=champion, opp_champion=opp_champion,
                                  queues=queues, require_opponent=bool(opp_champion))
    return {
        "keystones": _rune_breakdown(conn, base, params, "keystone"),
        "secondaries": _rune_breakdown(conn, base, params, "secondary_tree"),
    }


def comparison_entry(conn, puuid, my_champion=None, opp_champion=None, queues=None,
                     limit=20):
    """One side of a player comparison — either YOU (pass your tracked puuids)
    or a single comparison ('research') player. Reuses the same TOP-scoped base
    as every other stat, just pointed at whichever puuid(s) are asked for, so
    the two sides are always measured the same way.

      - scoped:  aggregate under the requested filter (+ metrics) — a matchup
                 (both champions), one champion, or everything
      - overall: that player's aggregate on my_champion regardless of opponent,
                 so a matchup reads against the champion baseline. None when
                 the scope isn't a matchup (nothing to compare it against).
      - recent:  their recent games in scope, with the runes/loadout they ran

    Aggregates come back even with zero games (games=0), so callers render
    "no games recorded" without a special case."""
    sbase, sparams = _filtered_base(puuid, champion=my_champion,
                                    opp_champion=opp_champion, queues=queues,
                                    require_opponent=bool(opp_champion))
    scoped = _pack_metrics(conn.execute(
        f"SELECT {_AGG}, {_metric_agg_select()} FROM ({sbase})", sparams).fetchone())
    overall = None
    if my_champion and opp_champion:
        obase, oparams = _filtered_base(puuid, champion=my_champion, queues=queues,
                                        require_opponent=False)
        overall = _pack_metrics(conn.execute(
            f"SELECT {_AGG}, {_metric_agg_select()} FROM ({obase})", oparams).fetchone())
    recent = [_decode_game_runes(r) for r in conn.execute(
        f"""SELECT match_id, game_creation_ms, game_duration_s, queue_id,
                   my_puuid, my_champion, opp_champion, rank_tier, win,
                   kills, deaths, assists, cs, my_runes_json, opp_runes_json,
                   spell1, spell2, my_items_json, my_starting_items_json, my_build_order_json,
                   my_skill_order_json
            FROM ({sbase}) ORDER BY game_creation_ms DESC LIMIT :limit""",
        {**sparams, "limit": limit})]
    return {"scoped": scoped, "overall": overall, "recent": recent}


def progress_segments(conn, puuids, sessions, champion=None, queues=None,
                      now_ms=None, baseline_days=30, side=None, roles=None):
    """Aggregate stats per period between coaching sessions.

    sessions: dicts with session_date ('YYYY-MM-DD') and title, any order.
    The returned segments expose the title under the 'note' key (existing
    rendering contract).
    Segments are half-open [from, to): games at a session's UTC midnight
    count toward the segment after that session. Returns [] without sessions.
    """
    if not sessions:
        return []
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    ordered = sorted(sessions, key=lambda s: s["session_date"])

    def date_ms(date_str):
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def session_ranks(session):
        raw = session["start_ranks"] if "start_ranks" in session.keys() else None
        return json.loads(raw) if raw else None

    bounds = [date_ms(s["session_date"]) for s in ordered]
    day_ms = 86_400_000
    segments = [
        # kept visible as the "before any coaching" reference; it anchors to no
        # session, so it carries no notes/coach
        {"label": "Baseline", "note": "", "from_ms": bounds[0] - baseline_days * day_ms,
         "to_ms": bounds[0], "start_ranks": None, "session_id": None,
         "session_date": "", "session_title": "", "coach": "", "link": "", "notes": ""},
    ]
    def anchored(session):
        """The session a period runs FROM — the row is "what happened after
        this session", so the row carries that session's own record."""
        keys = session.keys() if hasattr(session, "keys") else session
        return {"session_id": session["id"] if "id" in keys else None,
                "session_date": session["session_date"],
                "session_title": session["title"],
                "coach": session["coach"] if "coach" in keys else "",
                "link": session["link"] if "link" in keys else "",
                "notes": session["notes"] if "notes" in keys else ""}

    for i in range(len(ordered) - 1):
        segments.append({
            "label": f"{ordered[i]['session_date']} → {ordered[i + 1]['session_date']}",
            "note": ordered[i]["title"],
            "from_ms": bounds[i],
            "to_ms": bounds[i + 1],
            "start_ranks": session_ranks(ordered[i]),
            **anchored(ordered[i]),
        })
    segments.append({
        "label": f"Since {ordered[-1]['session_date']}",
        "note": ordered[-1]["title"],
        "from_ms": bounds[-1],
        "to_ms": now_ms,
        "start_ranks": session_ranks(ordered[-1]),
        **anchored(ordered[-1]),
    })

    results = []
    for segment in segments:
        base, params = _filtered_base(
            puuids, from_ms=segment["from_ms"], to_ms=segment["to_ms"] - 1,
            champion=champion, queues=queues, require_opponent=False, side=side)
        row = conn.execute(
            f"SELECT {_AGG}, {_metric_agg_select()} FROM ({base})", params).fetchone()
        totals = dict(row)
        metrics = {k: totals.pop(k) for k in metric_keys()}
        results.append({**segment, **totals, "metrics": metrics})
    return results


def segment_metrics(conn, puuids, from_ms=None, to_ms=None, champion=None, queues=None,
                    side=None, roles=None):
    """Aggregate coaching metrics over a period. NULLs are excluded per metric;
    metrics_games reports how many games have a metrics record at all."""
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms,
                                  champion=champion, queues=queues,
                                  require_opponent=False, side=side, roles=roles)
    row = conn.execute(
        f"""SELECT COUNT(*) AS games, COUNT(pm_match_id) AS metrics_games,
            {_metric_agg_select()}
            FROM ({base})""",
        params,
    ).fetchone()
    result = dict(row)
    return {
        "games": result.pop("games"),
        "metrics_games": result.pop("metrics_games"),
        "metrics": result,
    }


_BUCKET_EXPRS = {
    "day": "strftime('%Y-%m-%d', game_creation_ms/1000, 'unixepoch')",
    "week": "date(game_creation_ms/1000, 'unixepoch', 'weekday 0', '-6 days')",
    "month": "strftime('%Y-%m', game_creation_ms/1000, 'unixepoch')",
}


def trend_buckets(conn, puuids, bucket="month", from_ms=None, to_ms=None, champion=None,
                  queues=None, side=None, roles=None):
    """Base stats + coaching metrics grouped per calendar bucket, oldest first.
    Week buckets are labeled with their Monday's date. from_ms/to_ms optionally
    restrict to a period (default: full history, as before)."""
    if bucket not in _BUCKET_EXPRS:
        raise ValueError(f"bucket must be one of {sorted(_BUCKET_EXPRS)}")
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms, champion=champion,
                                  queues=queues, require_opponent=False, side=side, roles=roles)
    rows = conn.execute(
        f"""SELECT {_BUCKET_EXPRS[bucket]} AS bucket,
            COUNT(pm_match_id) AS metrics_games,
            {_AGG},
            {_metric_agg_select()}
            FROM ({base}) GROUP BY bucket ORDER BY bucket""",
        params,
    ).fetchall()
    results = []
    for row in rows:
        record = dict(row)
        metrics = {k: record.pop(k) for k in metric_keys()}
        record["metrics"] = metrics
        results.append(record)
    return results


def single_game_metrics(conn, match_id, puuid):
    """One game's metric values transformed to the same display units the
    aggregated views use. None when the game has no metrics row."""
    row = conn.execute(
        """SELECT pm.*, m.game_duration_s FROM participant_metrics pm
           JOIN matches m ON m.match_id = pm.match_id
           WHERE pm.match_id=? AND pm.puuid=?""",
        (match_id, puuid)).fetchone()
    if row is None:
        return None
    duration = row["game_duration_s"]
    values = {}
    for m in METRICS:
        raw = row[m["key"]]
        if raw is None:
            values[m["key"]] = None
        elif m["agg"] == "pct01":
            values[m["key"]] = 100.0 * raw
        elif m["agg"] == "per_min":
            values[m["key"]] = 60.0 * raw / duration
        elif m["agg"] == "pct_time":
            values[m["key"]] = 100.0 * raw / duration
        else:
            values[m["key"]] = raw
    return values


_FRAME_SERIES_COLS = ("cs", "xp", "gold", "level")


def _frame_series_side(conn, match_id, puuid):
    rows = conn.execute(
        """SELECT minute, cs, xp, gold, level FROM participant_frame_series
           WHERE match_id=? AND puuid=? ORDER BY minute""",
        (match_id, puuid)).fetchall()
    if not rows:
        return None
    return rows, {k: [r[k] for r in rows] for k in _FRAME_SERIES_COLS}


def game_curve(conn, match_id, puuid, opp_puuid=None):
    """Full-game per-minute gold/CS/XP/level series for one participant (and
    optionally their lane opponent) in a match — the full-game curve chart.
    None if `puuid` has no recorded series for that match (crawled before the
    feature existed, or the match's timeline was unavailable — run
    ./crawl.sh --backfill-frame-series). `opp` is None when opp_puuid isn't
    given or has no recorded series of its own."""
    mine = _frame_series_side(conn, match_id, puuid)
    if mine is None:
        return None
    rows, me = mine
    opp = None
    if opp_puuid:
        theirs = _frame_series_side(conn, match_id, opp_puuid)
        if theirs is not None:
            opp = theirs[1]
    return {"minutes": [r["minute"] for r in rows], "me": me, "opp": opp}


def block_games_detailed(conn):
    """Block-game entries hydrated from stored matches, oldest first."""
    rows = conn.execute(
        """SELECT bg.id AS entry_id, bg.block_id, bg.notes, bg.match_id, bg.puuid,
                  bg.weakside, bg.lane_result_7, bg.lane_result_14,
                  m.game_creation_ms, m.game_duration_s, m.queue_id,
                  m.jungle_start_100, m.jungle_start_200,
                  me.team_id AS my_team_id, me.team_position AS my_position,
                  me.champion_name AS my_champion, me.win,
                  me.kills, me.deaths, me.assists, me.cs,
                  pm.lane_adv_early, pm.lane_adv_late, pm.has_timeline,
                  pm.cs_diff_7, pm.level_diff_7, pm.gold_diff_7,
                  pm.cs_diff_14, pm.level_diff_14, pm.gold_diff_14,
                  opp.champion_name AS opp_champion,
                  myr.runes AS my_runes_json,
                  oppr.runes AS opp_runes_json
           FROM block_games bg
           JOIN participants me ON me.match_id = bg.match_id AND me.puuid = bg.puuid
           JOIN matches m ON m.match_id = bg.match_id
           LEFT JOIN participants opp ON opp.match_id = bg.match_id
               AND opp.team_id != me.team_id AND opp.team_position = me.team_position
               AND me.team_position != ''
           LEFT JOIN participant_metrics pm
               ON pm.match_id = bg.match_id AND pm.puuid = bg.puuid
           LEFT JOIN participant_runes myr
               ON myr.match_id = bg.match_id AND myr.puuid = bg.puuid
           LEFT JOIN participant_runes oppr
               ON oppr.match_id = bg.match_id AND oppr.puuid = opp.puuid
           ORDER BY m.game_creation_ms"""
    ).fetchall()
    return [_add_jungle_sides(_decode_game_runes(r)) for r in rows]


def _add_jungle_sides(game):
    """Derive strong/weak side for the player and their lane counterpart from
    the stored jungle start halves, and drop the raw columns the client has no
    use for. Both sides share a lane half, so the counterpart's verdict is the
    same comparison against the ENEMY jungler's start. Either can be None
    (mid/jungle, or a match whose jungle start was never determined) — the
    client falls back to the manual `weakside` flag then."""
    team = game.pop("my_team_id", None)
    position = game.pop("my_position", None)
    starts = {100: game.pop("jungle_start_100", None) or None,
              200: game.pop("jungle_start_200", None) or None}
    mine, theirs = starts.get(team), starts.get(200 if team == 100 else 100)
    game["my_jungle_half"] = mine
    game["opp_jungle_half"] = theirs
    game["auto_strongside"] = strongside(position, mine)
    game["opp_auto_strongside"] = strongside(position, theirs)
    return game


def _decode_game_runes(row):
    """Row from a query selecting my_runes_json/opp_runes_json -> dict with
    'runes' (mine) and 'opp_runes' (lane opponent's) decoded, or None."""
    game = dict(row)
    my_runes = game.pop("my_runes_json")
    opp_runes = game.pop("opp_runes_json")
    game["runes"] = json.loads(my_runes) if my_runes else None
    game["opp_runes"] = json.loads(opp_runes) if opp_runes else None
    # loadout is only selected by some queries; decode each when present
    if "my_items_json" in game.keys():
        items = game.pop("my_items_json")
        game["items"] = json.loads(items) if items else None
    if "my_starting_items_json" in game.keys():
        starting = game.pop("my_starting_items_json")
        game["starting_items"] = json.loads(starting) if starting else None
    if "my_build_order_json" in game.keys():
        build = game.pop("my_build_order_json")
        game["build_order"] = json.loads(build) if build else None
    if "my_skill_order_json" in game.keys():
        skill = game.pop("my_skill_order_json")
        game["skill_order"] = json.loads(skill) if skill else None
    return game


def games_in_range(conn, puuids, from_ms=None, to_ms=None, champion=None, queues=None,
                   opp_champion=None, rank_tier=None, side=None, roles=None):
    """Individual games (any role) for the tracked puuids, newest first."""
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms,
                                  champion=champion, queues=queues,
                                  rank_tier=rank_tier, opp_champion=opp_champion,
                                  require_opponent=False, side=side, roles=roles)
    sql = f"""
        SELECT match_id, game_creation_ms, game_duration_s, queue_id, my_puuid,
               my_champion, opp_champion, rank_tier, win,
               kills, deaths, assists, cs, lane_adv_early, lane_adv_late,
               my_runes_json, opp_runes_json, spell1, spell2, my_items_json,
               my_starting_items_json, my_build_order_json, my_skill_order_json
        FROM ({base}) ORDER BY game_creation_ms DESC
    """
    return [_decode_game_runes(r) for r in conn.execute(sql, params)]


def session_games_detailed(conn, session_id):
    """Games explicitly attached to a coaching session (session_games) — the
    Live coaching / VOD review 'Add game' picker — newest first."""
    sql = """
        SELECT sg.id AS session_game_id, sg.match_id, sg.puuid, sg.added_at_ms,
               m.game_creation_ms, m.game_duration_s, m.queue_id,
               me.champion_name AS my_champion, me.win,
               me.kills, me.deaths, me.assists, me.cs,
               opp.champion_name AS opp_champion,
               pl.game_name AS account,
               myr.runes AS my_runes_json,
               oppr.runes AS opp_runes_json
        FROM session_games sg
        JOIN participants me ON me.match_id = sg.match_id AND me.puuid = sg.puuid
        JOIN matches m ON m.match_id = sg.match_id
        LEFT JOIN participants opp ON opp.match_id = sg.match_id
            AND opp.team_id != me.team_id AND opp.team_position = me.team_position
            AND me.team_position != ''
        LEFT JOIN players pl ON pl.puuid = sg.puuid
        LEFT JOIN participant_runes myr ON myr.match_id = sg.match_id AND myr.puuid = sg.puuid
        LEFT JOIN participant_runes oppr ON oppr.match_id = sg.match_id AND oppr.puuid = opp.puuid
        WHERE sg.session_id = ?
        ORDER BY m.game_creation_ms DESC
    """
    return [_decode_game_runes(r) for r in conn.execute(sql, (session_id,))]


def review_queue(conn, puuid, limit=8):
    """Individual block games with no per-game notes yet — a lightweight
    "you played this, you haven't written anything down" nudge, across
    EVERY block (not just the current one).

    Scoped to games you added to a block (block_games) — the games you're
    actively practising — rather than every game ever played. One row per
    game where block_games.notes is still blank. Newest first."""
    base, params = _filtered_base(puuid)
    sql = f"""
        SELECT bg.id AS entry_id, bg.block_id, blk.title AS block_title,
               b.match_id, b.my_puuid AS puuid, b.my_champion, b.opp_champion,
               b.win, b.game_creation_ms
        FROM ({base}) b
        JOIN block_games bg ON bg.match_id = b.match_id AND bg.puuid = b.my_puuid
        JOIN blocks blk ON blk.id = bg.block_id
        WHERE bg.notes = ''
        ORDER BY b.game_creation_ms DESC
    """
    rows = [dict(r) for r in conn.execute(sql, params)]
    return rows[:limit]


def map_events(conn, puuids, from_ms=None, to_ms=None, champion=None, roles=None):
    """Death-location map events (see player_map_events) for the tracked
    puuids, filtered the same way as the other Trends-style queries (period/
    champion/role via _filtered_base). Explicitly deaths-only: the table also
    holds kills/towers/objectives for VOD chapters, and the heatmap must not
    plot those. match-v5's WARD_PLACED events carry no position (see
    CLAUDE.md), so there is still no ward counterpart."""
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms,
                                  champion=champion, require_opponent=False, roles=roles)
    sql = f"""
        SELECT pme.event_type, pme.x, pme.y, pme.timestamp_ms
        FROM player_map_events pme
        JOIN ({base}) b ON b.match_id = pme.match_id AND b.my_puuid = pme.puuid
        -- deaths only (the table also carries kills/towers for VOD chapters),
        -- and only rows that actually have a position: log-derived events
        -- carry timings but no coordinates
        WHERE pme.event_type = 'death' AND pme.x IS NOT NULL AND pme.y IS NOT NULL
        ORDER BY pme.timestamp_ms
    """
    return [dict(r) for r in conn.execute(sql, params)]


def champion_roles(conn, secondary_share=0.2):
    """Each champion's lane(s), computed empirically from EVERY participant in
    every stored match (not just tracked games) — {champion_id: [team_position,
    ...]}. A champion's dominant role is always included; a secondary role is
    added when it holds >= secondary_share of that champion's games (flex picks
    show under multiple lanes). Champions never seen are simply absent. Used by
    the tier-list pool's role filter."""
    rows = conn.execute(
        """SELECT champion_name, team_position, COUNT(*) AS c
           FROM participants WHERE team_position != ''
           GROUP BY champion_name, team_position""")
    dist = {}
    for r in rows:
        dist.setdefault(r["champion_name"], {})[r["team_position"]] = r["c"]
    out = {}
    for champ, d in dist.items():
        total = sum(d.values())
        top = max(d, key=d.get)
        roles = [pos for pos, c in d.items() if pos == top or c / total >= secondary_share]
        # stable order: TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY
        order = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}
        out[champ] = sorted(roles, key=lambda p: order.get(p, 9))
    return out
def filter_options(conn, puuid):
    base, params = _filtered_base(puuid, require_opponent=False)
    champions = [r[0] for r in conn.execute(
        f"SELECT DISTINCT my_champion FROM ({base}) ORDER BY my_champion", params)]
    queues = [r[0] for r in conn.execute(
        f"SELECT DISTINCT queue_id FROM ({base}) ORDER BY queue_id", params)]
    tiers = [r[0] for r in conn.execute(
        f"SELECT DISTINCT rank_tier FROM ({base})", params)]
    tier_order = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
                  "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER", "UNKNOWN"]
    tiers.sort(key=lambda t: tier_order.index(t) if t in tier_order else 99)
    return {"champions": champions, "queues": queues, "rank_tiers": tiers}
