import pytest

from server import db, stats

ME = "me-1"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    db.upsert_player(c, ME, "PlayerOne", "EUW", is_tracked=True)
    yield c
    c.close()


_counter = {"n": 0}


def add_match(conn, my_champ="Garen", opp_champ="Darius", win=True, when=1_700_000_000_000,
              queue=420, duration=1800, my_pos="TOP", opp_pos="TOP", opp_puuid=None,
              kills=6, deaths=3, assists=9, cs=210, gold=12000, dmg=18000, puuid=None):
    me = puuid or ME
    _counter["n"] += 1
    match_id = f"EUW1_{_counter['n']}"
    opp_puuid = opp_puuid or f"opp-{_counter['n']}"
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

    def part(puuid, champ, team, pos, w, **kw):
        return {
            "puuid": puuid, "riot_id_name": puuid, "champion_name": champ,
            "team_id": team, "team_position": pos, "win": int(w),
            "kills": kw.get("kills", 2), "deaths": kw.get("deaths", 2),
            "assists": kw.get("assists", 2), "cs": kw.get("cs", 150),
            "gold_earned": kw.get("gold", 10000),
            "damage_to_champions": kw.get("dmg", 10000),
        }

    parts = []
    for i, pos in enumerate(positions):
        if pos == my_pos:
            parts.append(part(me, my_champ, 100, pos, win, kills=kills, deaths=deaths,
                              assists=assists, cs=cs, gold=gold, dmg=dmg))
        else:
            parts.append(part(f"ally-{_counter['n']}-{i}", "Ahri", 100, pos, win))
    for i, pos in enumerate(positions):
        if pos == opp_pos:
            parts.append(part(opp_puuid, opp_champ, 200, pos, not win))
        else:
            # if the designated opponent isn't TOP, leave the enemy TOP slot
            # positionless so the match genuinely has no TOP opponent
            enemy_pos = "" if (pos == "TOP" and opp_pos != "TOP") else pos
            parts.append(part(f"enemy-{_counter['n']}-{i}", "Lux", 200, enemy_pos, not win))

    db.insert_match(
        conn,
        {"match_id": match_id, "queue_id": queue, "game_creation_ms": when,
         "game_duration_s": duration, "game_version": "14.1.1"},
        parts,
    )
    return match_id, opp_puuid


def test_matchups_basic_winrate(conn):
    add_match(conn, opp_champ="Darius", win=True)
    add_match(conn, opp_champ="Darius", win=True)
    add_match(conn, opp_champ="Darius", win=False)
    add_match(conn, opp_champ="Teemo", win=False)
    rows = stats.matchups(conn, ME)
    by_champ = {r["opp_champion"]: r for r in rows}
    assert by_champ["Darius"]["games"] == 3
    assert by_champ["Darius"]["wins"] == 2
    assert by_champ["Darius"]["winrate"] == pytest.approx(2 / 3)
    assert by_champ["Teemo"]["winrate"] == 0.0
    assert rows[0]["opp_champion"] == "Darius"  # sorted by games desc


def test_matchup_rates_and_kda(conn):
    add_match(conn, opp_champ="Darius", win=True, duration=1800,
              kills=6, deaths=3, assists=9, cs=210, gold=12000, dmg=18000)
    row = stats.matchups(conn, ME)[0]
    assert row["kda"] == pytest.approx((6 + 9) / 3)
    assert row["cs_min"] == pytest.approx(210 / 30)
    assert row["gold_min"] == pytest.approx(400.0)
    assert row["dmg_min"] == pytest.approx(600.0)
    assert row["avg_duration_s"] == 1800


def test_matchups_only_when_i_play_top(conn):
    add_match(conn, my_pos="MIDDLE", opp_champ="Zed")
    add_match(conn, my_pos="TOP", opp_champ="Darius")
    rows = stats.matchups(conn, ME)
    assert [r["opp_champion"] for r in rows] == ["Darius"]


def test_match_without_top_opponent_is_skipped(conn):
    add_match(conn, opp_pos="JUNGLE", opp_champ="Wukong")  # enemy has no TOP
    assert stats.matchups(conn, ME) == []


def test_remakes_excluded(conn):
    add_match(conn, opp_champ="Darius", duration=250)
    assert stats.matchups(conn, ME) == []


def test_date_range_filter(conn):
    add_match(conn, opp_champ="Old", when=1_000)
    add_match(conn, opp_champ="New", when=2_000_000)
    rows = stats.matchups(conn, ME, from_ms=1_000_000)
    assert [r["opp_champion"] for r in rows] == ["New"]
    rows = stats.matchups(conn, ME, to_ms=1_000_000)
    assert [r["opp_champion"] for r in rows] == ["Old"]


def test_own_champion_filter(conn):
    add_match(conn, my_champ="Garen", opp_champ="Darius")
    add_match(conn, my_champ="Kled", opp_champ="Teemo")
    rows = stats.matchups(conn, ME, champion="Kled")
    assert [r["opp_champion"] for r in rows] == ["Teemo"]


def test_queue_filter(conn):
    add_match(conn, queue=420, opp_champ="Darius")
    add_match(conn, queue=440, opp_champ="Teemo")
    rows = stats.matchups(conn, ME, queues=[440])
    assert [r["opp_champion"] for r in rows] == ["Teemo"]


def test_min_games_filter(conn):
    add_match(conn, opp_champ="Darius")
    add_match(conn, opp_champ="Darius")
    add_match(conn, opp_champ="Teemo")
    rows = stats.matchups(conn, ME, min_games=2)
    assert [r["opp_champion"] for r in rows] == ["Darius"]


def test_rank_tier_filter_uses_opponent_rank(conn):
    _, opp_a = add_match(conn, opp_champ="Darius", win=True)
    _, opp_b = add_match(conn, opp_champ="Teemo", win=False)
    db.set_player_rank(conn, opp_a, "GOLD", "II", 10, fetched_at_ms=1)
    rows = stats.matchups(conn, ME, rank_tier="GOLD")
    assert [r["opp_champion"] for r in rows] == ["Darius"]
    rows = stats.matchups(conn, ME, rank_tier="UNKNOWN")
    assert [r["opp_champion"] for r in rows] == ["Teemo"]


def test_matchups_by_rank_buckets(conn):
    _, opp_a = add_match(conn, opp_champ="Darius", win=True)
    _, opp_b = add_match(conn, opp_champ="Darius", win=False)
    db.set_player_rank(conn, opp_a, "GOLD", "II", 10, fetched_at_ms=1)
    db.set_player_rank(conn, opp_b, "PLATINUM", "IV", 20, fetched_at_ms=1)
    rows = stats.matchups_by_rank(conn, ME)
    buckets = {(r["rank_tier"], r["opp_champion"]): r for r in rows}
    assert buckets[("GOLD", "Darius")]["winrate"] == 1.0
    assert buckets[("PLATINUM", "Darius")]["winrate"] == 0.0


def test_summary_counts_and_champion_breakdown(conn):
    add_match(conn, my_champ="Garen", win=True)
    add_match(conn, my_champ="Garen", win=False)
    add_match(conn, my_champ="Kled", win=True)
    s = stats.summary(conn, ME)
    assert s["games"] == 3
    assert s["wins"] == 2
    assert s["winrate"] == pytest.approx(2 / 3)
    by_champ = {c["champion"]: c for c in s["by_champion"]}
    assert by_champ["Garen"]["games"] == 2
    assert len(s["recent"]) == 3
    assert s["recent"][0]["opp_champion"] in ("Darius",)


def test_summary_recent_includes_runes_actually_played(conn):
    match_id, opp_puuid = add_match(conn, opp_champ="Darius")
    s = stats.summary(conn, ME)
    assert s["recent"][0]["runes"] is None  # nothing recorded for this game yet
    assert s["recent"][0]["opp_runes"] is None
    db.insert_participant_runes(conn, match_id, ME, {
        "label": "", "primary_tree": "Precision", "keystone": "Conqueror",
        "primary_runes": ["Triumph", "Legend: Alacrity", "Last Stand"],
        "secondary_tree": "Resolve", "secondary_runes": ["Bone Plating", "Overgrowth"],
        "shards": ["Adaptive Force", "Armor", "Health"],
    })
    db.insert_participant_runes(conn, match_id, opp_puuid, {
        "label": "", "primary_tree": "Resolve", "keystone": "Grasp of the Undying",
        "primary_runes": ["Demolish", "Second Wind", "Overgrowth"],
        "secondary_tree": "Inspiration", "secondary_runes": ["Biscuit Delivery", "Cosmic Insight"],
        "shards": ["Health", "Armor", "Health"],
    })
    s = stats.summary(conn, ME)
    assert s["recent"][0]["runes"]["keystone"] == "Conqueror"
    assert s["recent"][0]["opp_runes"]["keystone"] == "Grasp of the Undying"


DAY_MS = 86_400_000
# 2026-06-28 00:00 UTC
S1_MS = 1_782_604_800_000
NOW_MS = S1_MS + 5 * DAY_MS  # "2026-07-03"


def sessions(*dates_titles):
    return [{"session_date": d, "title": t} for d, t in dates_titles]


def test_progress_single_session_gives_baseline_and_since(conn):
    add_match(conn, when=S1_MS - 2 * DAY_MS, win=True)    # baseline
    add_match(conn, when=S1_MS + DAY_MS, win=False)       # since session
    segments = stats.progress_segments(conn, [ME], sessions(("2026-06-28", "waves")),
                                       now_ms=NOW_MS)
    assert len(segments) == 2
    baseline, since = segments
    assert baseline["label"] == "Baseline"
    assert baseline["games"] == 1 and baseline["winrate"] == 1.0
    assert baseline["from_ms"] == S1_MS - 30 * DAY_MS
    assert baseline["to_ms"] == S1_MS
    assert since["games"] == 1 and since["winrate"] == 0.0
    assert since["note"] == "waves"
    assert since["to_ms"] == NOW_MS


def test_progress_boundary_is_utc_midnight_of_session_date(conn):
    add_match(conn, when=S1_MS - 1, win=True)   # 1 ms before midnight -> baseline
    add_match(conn, when=S1_MS, win=False)      # exactly midnight -> after
    segments = stats.progress_segments(conn, [ME], sessions(("2026-06-28", "")),
                                       now_ms=NOW_MS)
    assert segments[0]["games"] == 1 and segments[0]["winrate"] == 1.0
    assert segments[1]["games"] == 1 and segments[1]["winrate"] == 0.0


def test_progress_two_sessions_three_segments_with_empty_middle(conn):
    add_match(conn, when=S1_MS - DAY_MS)                      # baseline
    add_match(conn, when=S1_MS + 8 * DAY_MS)                  # after session 2
    segments = stats.progress_segments(
        conn, [ME], sessions(("2026-06-28", "a"), ("2026-07-05", "b")),
        now_ms=S1_MS + 10 * DAY_MS)
    assert [s["label"] for s in segments] == [
        "Baseline", "2026-06-28 → 2026-07-05", "Since 2026-07-05"]
    assert segments[1]["games"] == 0
    assert segments[1]["winrate"] is None
    assert segments[2]["games"] == 1


def test_progress_unions_multiple_puuids(conn):
    db.upsert_player(conn, "me-2", "PlayerTwo", "EUW", is_tracked=True)
    add_match(conn, when=S1_MS + DAY_MS, win=True)
    # second account's game in the same window
    global _counter
    from tests.test_stats import _counter as counter
    match_id, _ = add_match(conn, when=S1_MS + DAY_MS, win=False)
    conn.execute("UPDATE participants SET puuid='me-2' WHERE match_id=? AND puuid=?",
                 (match_id, ME))
    conn.commit()
    segments = stats.progress_segments(conn, [ME, "me-2"], sessions(("2026-06-28", "")),
                                       now_ms=NOW_MS)
    assert segments[1]["games"] == 2
    assert segments[1]["winrate"] == 0.5


def test_progress_champion_filter(conn):
    add_match(conn, when=S1_MS + DAY_MS, my_champ="Gwen", win=True)
    add_match(conn, when=S1_MS + DAY_MS, my_champ="Garen", win=False)
    segments = stats.progress_segments(conn, [ME], sessions(("2026-06-28", "")),
                                       champion="Gwen", now_ms=NOW_MS)
    assert segments[1]["games"] == 1
    assert segments[1]["winrate"] == 1.0


def test_games_in_range_bounds_and_order(conn):
    add_match(conn, when=1_000, opp_champ="Old")
    add_match(conn, when=5_000, opp_champ="Mid")
    add_match(conn, when=9_000, opp_champ="New")
    games = stats.games_in_range(conn, [ME], from_ms=2_000, to_ms=9_000)
    assert [g["opp_champion"] for g in games] == ["New", "Mid"]  # newest first
    assert games[0]["match_id"].startswith("EUW1_")
    assert games[0]["my_puuid"] == ME
    assert {"win", "kills", "deaths", "assists", "cs", "game_duration_s",
            "queue_id", "rank_tier"} <= set(games[0].keys())


def test_games_in_range_unions_accounts(conn):
    db.upsert_player(conn, "me-2", "PlayerTwo", "EUW", is_tracked=True)
    add_match(conn, when=1_000)
    match_id, _ = add_match(conn, when=2_000)
    conn.execute("UPDATE participants SET puuid='me-2' WHERE match_id=? AND puuid=?",
                 (match_id, ME))
    conn.commit()
    games = stats.games_in_range(conn, [ME, "me-2"])
    assert [g["my_puuid"] for g in games] == ["me-2", ME]


def test_games_in_range_filters_and_remakes(conn):
    add_match(conn, when=1_000, my_champ="Gwen", opp_champ="Darius")
    add_match(conn, when=2_000, my_champ="Garen", opp_champ="Teemo")
    add_match(conn, when=3_000, my_champ="Gwen", opp_champ="Sett", duration=250)  # remake
    games = stats.games_in_range(conn, [ME], champion="Gwen")
    assert [g["opp_champion"] for g in games] == ["Darius"]


def test_games_in_range_filters_by_opponent_and_rank(conn):
    add_match(conn, when=1_000, opp_champ="Darius")
    _, opp = add_match(conn, when=2_000, opp_champ="Teemo")
    db.set_player_rank(conn, opp, "GOLD", "I", 1, fetched_at_ms=1)
    games = stats.games_in_range(conn, [ME], opp_champion="Darius")
    assert [g["opp_champion"] for g in games] == ["Darius"]
    games = stats.games_in_range(conn, [ME], rank_tier="GOLD")
    assert [g["opp_champion"] for g in games] == ["Teemo"]


def test_games_in_range_lists_game_without_top_opponent(conn):
    add_match(conn, when=1_000, opp_pos="JUNGLE", opp_champ="Wukong")
    games = stats.games_in_range(conn, [ME])
    assert len(games) == 1
    assert games[0]["opp_champion"] is None


def test_games_in_range_includes_runes_actually_played(conn):
    match_id, opp_puuid = add_match(conn, when=1_000, opp_champ="Darius")
    games = stats.games_in_range(conn, [ME])
    assert games[0]["runes"] is None  # nothing recorded for this game yet
    assert games[0]["opp_runes"] is None
    db.insert_participant_runes(conn, match_id, ME, {
        "label": "", "primary_tree": "Precision", "keystone": "Conqueror",
        "primary_runes": ["Triumph", "Legend: Alacrity", "Last Stand"],
        "secondary_tree": "Resolve", "secondary_runes": ["Bone Plating", "Overgrowth"],
        "shards": ["Adaptive Force", "Armor", "Health"],
    })
    db.insert_participant_runes(conn, match_id, opp_puuid, {
        "label": "", "primary_tree": "Resolve", "keystone": "Grasp of the Undying",
        "primary_runes": ["Demolish", "Second Wind", "Overgrowth"],
        "secondary_tree": "Inspiration", "secondary_runes": ["Biscuit Delivery", "Cosmic Insight"],
        "shards": ["Health", "Armor", "Health"],
    })
    games = stats.games_in_range(conn, [ME])
    assert games[0]["runes"]["keystone"] == "Conqueror"
    assert games[0]["opp_runes"]["keystone"] == "Grasp of the Undying"


def test_progress_segments_carry_session_start_ranks(conn):
    import json
    add_match(conn, when=S1_MS + DAY_MS)
    session_rows = [{"session_date": "2026-06-28", "title": "t",
                     "start_ranks": json.dumps([{"account": "A#EUW", "tier": "GOLD",
                                                 "division": "I", "lp": 10}])}]
    segments = stats.progress_segments(conn, [ME], session_rows, now_ms=NOW_MS)
    assert segments[0]["start_ranks"] is None            # baseline
    assert segments[1]["start_ranks"][0]["tier"] == "GOLD"


def test_progress_no_sessions_returns_empty(conn):
    add_match(conn)
    assert stats.progress_segments(conn, [ME], [], now_ms=NOW_MS) == []


def add_metrics(conn, match_id, puuid=ME, **overrides):
    from server.metrics import metric_keys
    values = {k: None for k in metric_keys()}
    values["has_challenges"] = 1
    values.update(overrides)
    db.insert_participant_metrics(conn, match_id, puuid, values)


def test_segment_metrics_averages_and_pct(conn):
    m1, _ = add_match(conn, when=1_000, duration=1800)
    m2, _ = add_match(conn, when=2_000, duration=1800)
    add_metrics(conn, m1, cs_at_10=80, lane_adv_early=1, team_dmg_pct=0.20, time_dead=180)
    add_metrics(conn, m2, cs_at_10=90, lane_adv_early=0, team_dmg_pct=0.30, time_dead=360)
    result = stats.segment_metrics(conn, [ME])
    assert result["games"] == 2
    assert result["metrics_games"] == 2
    metrics = result["metrics"]
    assert metrics["cs_at_10"] == pytest.approx(85.0)
    assert metrics["lane_adv_early"] == pytest.approx(50.0)   # pct01 -> %
    assert metrics["team_dmg_pct"] == pytest.approx(25.0)
    assert metrics["time_dead"] == pytest.approx(100 * 540 / 3600)  # pct_time


def test_segment_metrics_ignores_null_rows_and_reports_coverage(conn):
    m1, _ = add_match(conn, when=1_000)
    m2, _ = add_match(conn, when=2_000)   # no metrics row at all
    add_metrics(conn, m1, cs_at_10=80)
    result = stats.segment_metrics(conn, [ME])
    assert result["games"] == 2
    assert result["metrics_games"] == 1
    assert result["metrics"]["cs_at_10"] == pytest.approx(80.0)
    assert result["metrics"]["vision_adv"] is None  # never present


def test_segment_metrics_per_min_only_counts_rows_with_value(conn):
    m1, _ = add_match(conn, when=1_000, duration=1800)
    m2, _ = add_match(conn, when=2_000, duration=3600)  # no metrics row
    add_metrics(conn, m1, self_mitigated=18000)
    result = stats.segment_metrics(conn, [ME])
    # per_min must divide by the 1800s of the covered game only
    assert result["metrics"]["self_mitigated"] == pytest.approx(600.0)


def test_segment_metrics_respects_filters(conn):
    m1, _ = add_match(conn, when=1_000, my_champ="Gwen")
    m2, _ = add_match(conn, when=2_000, my_champ="Garen")
    add_metrics(conn, m1, cs_at_10=80)
    add_metrics(conn, m2, cs_at_10=40)
    result = stats.segment_metrics(conn, [ME], champion="Gwen")
    assert result["metrics"]["cs_at_10"] == pytest.approx(80.0)


DAY = 86_400_000


def test_trend_buckets_month(conn):
    jan = 1_704_067_200_000  # 2024-01-01 UTC
    feb = 1_706_745_600_000  # 2024-02-01 UTC
    m1, _ = add_match(conn, when=jan, win=True)
    m2, _ = add_match(conn, when=jan + DAY, win=False)
    m3, _ = add_match(conn, when=feb, win=True)
    add_metrics(conn, m1, cs_at_10=80)
    add_metrics(conn, m2, cs_at_10=90)
    add_metrics(conn, m3, cs_at_10=60)
    buckets = stats.trend_buckets(conn, [ME], bucket="month")
    assert [b["bucket"] for b in buckets] == ["2024-01", "2024-02"]
    assert buckets[0]["games"] == 2
    assert buckets[0]["winrate"] == pytest.approx(0.5)
    assert buckets[0]["metrics"]["cs_at_10"] == pytest.approx(85.0)
    assert buckets[1]["metrics"]["cs_at_10"] == pytest.approx(60.0)


def test_trend_buckets_week_starts_monday(conn):
    # 2024-01-03 is a Wednesday; its week bucket is Monday 2024-01-01
    wed = 1_704_240_000_000
    add_match(conn, when=wed)
    buckets = stats.trend_buckets(conn, [ME], bucket="week")
    assert buckets[0]["bucket"] == "2024-01-01"
    # Sunday 2024-01-07 belongs to the same week; Monday 2024-01-08 doesn't
    add_match(conn, when=wed + 4 * DAY)
    add_match(conn, when=wed + 5 * DAY)
    buckets = stats.trend_buckets(conn, [ME], bucket="week")
    assert [b["bucket"] for b in buckets] == ["2024-01-01", "2024-01-08"]
    assert buckets[0]["games"] == 2


def test_trend_buckets_day_and_bad_bucket(conn):
    add_match(conn, when=1_704_067_200_000)
    buckets = stats.trend_buckets(conn, [ME], bucket="day")
    assert buckets[0]["bucket"] == "2024-01-01"
    with pytest.raises(ValueError):
        stats.trend_buckets(conn, [ME], bucket="year")


def test_block_games_detailed_hydrates_from_matches(conn):
    m1, _ = add_match(conn, my_champ="Gwen", opp_champ="Darius", win=True, when=5_000,
                      kills=7, deaths=2, assists=4, cs=240, duration=1800)
    m2, _ = add_match(conn, my_champ="Kled", opp_pos="JUNGLE", opp_champ="Wukong",
                      win=False, when=3_000)
    add_metrics(conn, m1, lane_adv_early=1, lane_adv_late=0)
    db.add_game_to_block(conn, m1, ME)
    db.add_game_to_block(conn, m2, ME)
    games = stats.block_games_detailed(conn)
    assert [g["match_id"] for g in games] == [m2, m1]  # game_creation order
    g1 = next(g for g in games if g["match_id"] == m1)
    assert g1["my_champion"] == "Gwen"
    assert g1["opp_champion"] == "Darius"
    assert (g1["win"], g1["kills"], g1["deaths"], g1["assists"]) == (1, 7, 2, 4)
    assert g1["cs"] == 240
    assert g1["lane_adv_early"] == 1
    assert g1["lane_adv_late"] == 0
    assert g1["block_id"] == 1
    assert g1["notes"] == ""
    g2 = next(g for g in games if g["match_id"] == m2)
    assert g2["opp_champion"] is None  # no enemy TOP in that game
    assert g2["lane_adv_early"] is None  # no metrics row for that game
    assert g1["runes"] is None and g1["opp_runes"] is None  # nothing recorded yet


def test_block_games_detailed_includes_runes_actually_played(conn):
    match_id, opp_puuid = add_match(conn, opp_champ="Darius")
    db.add_game_to_block(conn, match_id, ME)
    db.insert_participant_runes(conn, match_id, ME, {
        "label": "", "primary_tree": "Precision", "keystone": "Conqueror",
        "primary_runes": ["Triumph", "Legend: Alacrity", "Last Stand"],
        "secondary_tree": "Resolve", "secondary_runes": ["Bone Plating", "Overgrowth"],
        "shards": ["Adaptive Force", "Armor", "Health"],
    })
    db.insert_participant_runes(conn, match_id, opp_puuid, {
        "label": "", "primary_tree": "Resolve", "keystone": "Grasp of the Undying",
        "primary_runes": ["Demolish", "Second Wind", "Overgrowth"],
        "secondary_tree": "Inspiration", "secondary_runes": ["Biscuit Delivery", "Cosmic Insight"],
        "shards": ["Health", "Armor", "Health"],
    })
    games = stats.block_games_detailed(conn)
    assert games[0]["runes"]["keystone"] == "Conqueror"
    assert games[0]["opp_runes"]["keystone"] == "Grasp of the Undying"


def test_block_games_detailed_includes_lane_deltas_and_timeline_flag(conn):
    match_id, _ = add_match(conn, opp_champ="Darius")
    db.add_game_to_block(conn, match_id, ME)
    # no metrics row yet -> has_timeline null, deltas null (the ⏳ state client-side)
    g = stats.block_games_detailed(conn)[0]
    assert g["has_timeline"] is None and g["cs_diff_7"] is None
    add_metrics(conn, match_id, has_timeline=1, cs_diff_7=15.0, level_diff_7=1.0,
                gold_diff_7=420.0, cs_diff_14=33.0, level_diff_14=2.0, gold_diff_14=900.0)
    g = stats.block_games_detailed(conn)[0]
    assert g["has_timeline"] == 1
    assert g["cs_diff_7"] == 15.0 and g["gold_diff_14"] == 900.0


def test_single_game_metrics_transforms_per_agg_kind(conn):
    m1, _ = add_match(conn, when=1_000, duration=1800)
    add_metrics(conn, m1, cs_at_10=80, lane_adv_early=1, team_dmg_pct=0.25,
                self_mitigated=18000, time_dead=180)
    metrics = stats.single_game_metrics(conn, m1, ME)
    assert metrics["cs_at_10"] == 80                       # avg -> raw
    assert metrics["lane_adv_early"] == 100.0              # pct01 -> %
    assert metrics["team_dmg_pct"] == pytest.approx(25.0)
    assert metrics["self_mitigated"] == pytest.approx(600.0)   # per_min
    assert metrics["time_dead"] == pytest.approx(10.0)         # pct_time
    assert metrics["vision_adv"] is None                   # missing field


def test_single_game_metrics_missing_row_returns_none(conn):
    m1, _ = add_match(conn, when=1_000)
    assert stats.single_game_metrics(conn, m1, ME) is None
    assert stats.single_game_metrics(conn, "EUW1_nope", ME) is None


def test_filter_options(conn):
    _, opp = add_match(conn, my_champ="Garen", queue=420)
    add_match(conn, my_champ="Kled", queue=440)
    db.set_player_rank(conn, opp, "GOLD", "II", 10, fetched_at_ms=1)
    opts = stats.filter_options(conn, ME)
    assert set(opts["champions"]) == {"Garen", "Kled"}
    assert set(opts["queues"]) == {420, 440}
    assert "GOLD" in opts["rank_tiers"] and "UNKNOWN" in opts["rank_tiers"]


def test_rank_value_absolute_ladder_points():
    assert stats.rank_value("IRON", "IV", 0) == 0
    assert stats.rank_value("GOLD", "II", 54) == 1200 + 200 + 54
    assert stats.rank_value("EMERALD", "I", 10) == 2000 + 300 + 10
    assert stats.rank_value("MASTER", None, 120) == 2800 + 120
    assert stats.rank_value("GRANDMASTER", None, 600) == 2800 + 600
    assert stats.rank_value("PLATINUM", None, None) == 1600  # missing bits tolerated
    assert stats.rank_value(None, None, None) is None
    assert stats.rank_value("WOOD", "IV", 10) is None


def test_rank_history_series_per_puuid(conn):
    db.record_rank_history(conn, ME, "GOLD", "II", 40, 1000)
    db.record_rank_history(conn, ME, None, None, None, 1500)  # unranked: skipped
    db.record_rank_history(conn, ME, "GOLD", "I", 10, 2000)
    db.record_rank_history(conn, "other", "SILVER", "IV", 0, 900)
    series = stats.rank_history(conn, [ME])
    assert list(series) == [ME]
    assert [(p["t"], p["value"]) for p in series[ME]] == [
        (1000, 1440), (2000, 1510)]
    assert series[ME][0]["tier"] == "GOLD" and series[ME][0]["division"] == "II"
    assert stats.rank_history(conn, []) == {}


def test_value_to_rank_inverse():
    assert stats.value_to_rank(1454) == ("GOLD", "II", 54)
    assert stats.value_to_rank(0) == ("IRON", "IV", 0)
    assert stats.value_to_rank(2950) == ("MASTER", None, 150)
    assert stats.value_to_rank(-40) == ("IRON", "IV", 0)  # clamped


def test_rank_history_estimates_from_ranked_results(conn):
    add_match(conn, win=True, when=1_000_000, queue=420)    # before first anchor
    add_match(conn, win=False, when=3_000_000, queue=420)   # between anchors
    add_match(conn, win=True, when=3_500_000, queue=420)    # between anchors
    add_match(conn, win=True, when=6_000_000, queue=420)    # after last anchor
    add_match(conn, win=True, when=6_500_000, queue=440)    # flex: ignored
    add_match(conn, win=True, when=7_000_000, queue=420, duration=200)  # remake: ignored
    db.record_rank_history(conn, ME, "GOLD", "II", 40, 2_000_000)  # 1440
    db.record_rank_history(conn, ME, "GOLD", "II", 10, 5_000_000)  # 1410
    pts = stats.rank_history(conn, [ME])[ME]
    assert [(p["t"], p["value"], p["estimated"]) for p in pts] == [
        (1_000_000, 1440, True),   # backward walk: value right after that win
        (2_000_000, 1440, False),
        (3_000_000, 1420, True),   # loss -20
        (3_500_000, 1440, True),   # win +20
        (5_000_000, 1410, False),  # real snapshot resets the drift
        (6_000_000, 1430, True),   # forward walk from the last anchor
    ]
    assert (pts[2]["tier"], pts[2]["division"], pts[2]["lp"]) == ("GOLD", "II", 20)


def test_rank_history_no_anchor_no_estimates(conn):
    add_match(conn, win=True, when=1_000_000, queue=420)
    assert stats.rank_history(conn, [ME]) == {ME: []}


def test_games_in_range_includes_lane_metrics(conn):
    m1, _ = add_match(conn, when=1_000)
    add_metrics(conn, m1, lane_adv_early=1, lane_adv_late=0)
    m2, _ = add_match(conn, when=2_000)  # no metrics row -> nulls
    games = stats.games_in_range(conn, [ME])
    by_id = {g["match_id"]: g for g in games}
    assert (by_id[m1]["lane_adv_early"], by_id[m1]["lane_adv_late"]) == (1, 0)
    assert by_id[m2]["lane_adv_early"] is None
