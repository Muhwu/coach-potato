"""Aggregated top-lane statistics.

All functions operate on the tracked player's TOP-lane games, excluding
remakes (< 300 s). The lane opponent is the enemy participant with
team_position='TOP'; rank buckets use the opponent's current solo rank
('UNKNOWN' when never fetched / unranked).
"""
import json
import time
from datetime import datetime, timezone

from .metrics import METRICS, metric_keys

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
       opp.champion_name AS opp_champion, opp.puuid AS opp_puuid,
       COALESCE(pr.solo_tier, 'UNKNOWN') AS rank_tier,
       pm.match_id AS pm_match_id,
       myr.runes AS my_runes_json,
       oppr.runes AS opp_runes_json,
       """ + _METRIC_SELECT + """
FROM participants me
JOIN matches m ON m.match_id = me.match_id
LEFT JOIN participants opp ON opp.match_id = me.match_id
    AND opp.team_id != me.team_id AND opp.team_position = 'TOP'
LEFT JOIN player_ranks pr ON pr.puuid = opp.puuid
LEFT JOIN participant_metrics pm
    ON pm.match_id = me.match_id AND pm.puuid = me.puuid
LEFT JOIN participant_runes myr
    ON myr.match_id = me.match_id AND myr.puuid = me.puuid
LEFT JOIN participant_runes oppr
    ON oppr.match_id = me.match_id AND oppr.puuid = opp.puuid
WHERE me.puuid IN ({puuid_slots}) AND me.team_position = 'TOP'
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


def _filtered_base(puuid, from_ms=None, to_ms=None, champion=None, queues=None,
                   rank_tier=None, require_opponent=True, opp_champion=None):
    puuids = [puuid] if isinstance(puuid, str) else list(puuid)
    sql = _BASE.format(puuid_slots=",".join(f":puuid{i}" for i in range(len(puuids))))
    params = {"remake_s": REMAKE_S}
    params.update({f"puuid{i}": p for i, p in enumerate(puuids)})
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


def matchups(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
             rank_tier=None, min_games=1):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier)
    params["min_games"] = min_games
    sql = f"""
        SELECT opp_champion, {_AGG}
        FROM ({base})
        GROUP BY opp_champion
        HAVING COUNT(*) >= :min_games
        ORDER BY games DESC, winrate DESC
    """
    return [dict(r) for r in conn.execute(sql, params)]


def matchups_by_rank(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
                     rank_tier=None, min_games=1):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier)
    params["min_games"] = min_games
    sql = f"""
        SELECT rank_tier, opp_champion, {_AGG}
        FROM ({base})
        GROUP BY rank_tier, opp_champion
        HAVING COUNT(*) >= :min_games
        ORDER BY rank_tier, games DESC
    """
    return [dict(r) for r in conn.execute(sql, params)]


def summary(conn, puuid, from_ms=None, to_ms=None, champion=None, queues=None,
            rank_tier=None, min_games=1):
    base, params = _filtered_base(puuid, from_ms, to_ms, champion, queues, rank_tier,
                                  require_opponent=False)
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
                   my_puuid, my_champion, opp_champion, rank_tier, win,
                   kills, deaths, assists, cs, my_runes_json, opp_runes_json
            FROM ({base}) ORDER BY game_creation_ms DESC LIMIT 20""",
        params)]
    result = dict(totals) if totals["games"] else {"games": 0, "wins": 0, "winrate": None}
    result["by_champion"] = by_champion
    result["recent"] = recent
    return result


def progress_segments(conn, puuids, sessions, champion=None, queues=None,
                      now_ms=None, baseline_days=30):
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
        {"label": "Baseline", "note": "", "from_ms": bounds[0] - baseline_days * day_ms,
         "to_ms": bounds[0], "start_ranks": None},
    ]
    for i in range(len(ordered) - 1):
        segments.append({
            "label": f"{ordered[i]['session_date']} → {ordered[i + 1]['session_date']}",
            "note": ordered[i]["title"],
            "from_ms": bounds[i],
            "to_ms": bounds[i + 1],
            "start_ranks": session_ranks(ordered[i]),
        })
    segments.append({
        "label": f"Since {ordered[-1]['session_date']}",
        "note": ordered[-1]["title"],
        "from_ms": bounds[-1],
        "to_ms": now_ms,
        "start_ranks": session_ranks(ordered[-1]),
    })

    results = []
    for segment in segments:
        base, params = _filtered_base(
            puuids, from_ms=segment["from_ms"], to_ms=segment["to_ms"] - 1,
            champion=champion, queues=queues, require_opponent=False)
        totals = dict(conn.execute(f"SELECT {_AGG} FROM ({base})", params).fetchone())
        results.append({**segment, **totals})
    return results


def segment_metrics(conn, puuids, from_ms=None, to_ms=None, champion=None, queues=None):
    """Aggregate coaching metrics over a period. NULLs are excluded per metric;
    metrics_games reports how many games have a metrics record at all."""
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms,
                                  champion=champion, queues=queues,
                                  require_opponent=False)
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


def trend_buckets(conn, puuids, bucket="month", champion=None, queues=None):
    """Base stats + coaching metrics grouped per calendar bucket, oldest first.
    Week buckets are labeled with their Monday's date."""
    if bucket not in _BUCKET_EXPRS:
        raise ValueError(f"bucket must be one of {sorted(_BUCKET_EXPRS)}")
    base, params = _filtered_base(puuids, champion=champion, queues=queues,
                                  require_opponent=False)
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


def block_games_detailed(conn):
    """Block-game entries hydrated from stored matches, oldest first."""
    rows = conn.execute(
        """SELECT bg.id AS entry_id, bg.block_id, bg.notes, bg.match_id, bg.puuid,
                  m.game_creation_ms, m.game_duration_s, m.queue_id,
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
               AND opp.team_id != me.team_id AND opp.team_position = 'TOP'
           LEFT JOIN participant_metrics pm
               ON pm.match_id = bg.match_id AND pm.puuid = bg.puuid
           LEFT JOIN participant_runes myr
               ON myr.match_id = bg.match_id AND myr.puuid = bg.puuid
           LEFT JOIN participant_runes oppr
               ON oppr.match_id = bg.match_id AND oppr.puuid = opp.puuid
           ORDER BY m.game_creation_ms"""
    ).fetchall()
    return [_decode_game_runes(r) for r in rows]


def _decode_game_runes(row):
    """Row from a query selecting my_runes_json/opp_runes_json -> dict with
    'runes' (mine) and 'opp_runes' (lane opponent's) decoded, or None."""
    game = dict(row)
    my_runes = game.pop("my_runes_json")
    opp_runes = game.pop("opp_runes_json")
    game["runes"] = json.loads(my_runes) if my_runes else None
    game["opp_runes"] = json.loads(opp_runes) if opp_runes else None
    return game


def games_in_range(conn, puuids, from_ms=None, to_ms=None, champion=None, queues=None,
                   opp_champion=None, rank_tier=None):
    """Individual top-lane games for the tracked puuids, newest first."""
    base, params = _filtered_base(puuids, from_ms=from_ms, to_ms=to_ms,
                                  champion=champion, queues=queues,
                                  rank_tier=rank_tier, opp_champion=opp_champion,
                                  require_opponent=False)
    sql = f"""
        SELECT match_id, game_creation_ms, game_duration_s, queue_id, my_puuid,
               my_champion, opp_champion, rank_tier, win,
               kills, deaths, assists, cs, lane_adv_early, lane_adv_late,
               my_runes_json, opp_runes_json
        FROM ({base}) ORDER BY game_creation_ms DESC
    """
    return [_decode_game_runes(r) for r in conn.execute(sql, params)]


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
