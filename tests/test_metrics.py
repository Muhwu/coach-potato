from server.metrics import (METRICS, map_half, metric_keys, parse_build_order,
                            parse_death_events, parse_jungle_starts, parse_metrics,
                            parse_skill_order, parse_starting_items,
                            parse_timeline_deltas, strongside)


def sample_match(puuid="p1", challenges=True):
    participant = {
        "puuid": puuid,
        "damageSelfMitigated": 25320,
        "damageDealtToTurrets": 8353,
        "totalTimeSpentDead": 259,
    }
    if challenges:
        participant["challenges"] = {
            "laneMinionsFirst10Minutes": 87,
            "earlyLaningPhaseGoldExpAdvantage": 1,
            "laningPhaseGoldExpAdvantage": 0,
            "maxCsAdvantageOnLaneOpponent": 95,
            "maxLevelLeadLaneOpponent": 2,
            "turretPlatesTaken": 10,
            "soloKills": 2,
            "takedownsFirstXMinutes": 3,
            "teamDamagePercentage": 0.187,
            "killParticipation": 0.242,
            "damageTakenOnTeamPercentage": 0.161,
            "skillshotsDodged": 63,
            "turretTakedowns": 2,
            "teleportTakedowns": 1,
            "riftHeraldTakedowns": 0,
            "visionScorePerMinute": 0.674,
            "visionScoreAdvantageLaneOpponent": -0.439,
            "controlWardsPlaced": 0,
            "wardTakedowns": 2,
        }
    return {"info": {"participants": [participant]}}


def test_registry_shape():
    assert len(METRICS) >= 20
    groups = {m["group"] for m in METRICS}
    assert groups == {"Laning", "Damage & fighting", "Objectives & map", "Vision & survival"}
    for m in METRICS:
        assert m["agg"] in ("avg", "pct01", "per_min", "pct_time")
        assert m["direction"] in (1, -1, 0)
    assert len(metric_keys()) == len(set(metric_keys()))


def test_parse_metrics_extracts_all_fields():
    values = parse_metrics(sample_match(), "p1")
    assert values["has_challenges"] == 1
    assert values["cs_at_10"] == 87
    assert values["lane_adv_early"] == 1
    assert values["lane_adv_late"] == 0
    assert values["team_dmg_pct"] == 0.187
    assert values["self_mitigated"] == 25320   # participant-level source
    assert values["turret_damage"] == 8353
    assert values["time_dead"] == 259
    assert values["vision_adv"] == -0.439
    assert set(values) == set(metric_keys()) | {"has_challenges"}
    # timeline-sourced metrics are unknown at parse time (no timeline here)
    assert values["cs_diff_7"] is None
    assert values["gold_diff_14"] is None


def _timeline(me_pid=1, opp_pid=6, frames=None):
    return {"info": {
        "participants": [{"participantId": me_pid, "puuid": "me"},
                         {"participantId": opp_pid, "puuid": "opp"}],
        "frames": frames or [],
    }}


def _frame(ts, pids):
    # pids: {participantId: (cs, jungleCs, level, gold)}
    return {"timestamp": ts, "participantFrames": {
        str(pid): {"minionsKilled": cs, "jungleMinionsKilled": jg,
                   "level": lvl, "totalGold": gold}
        for pid, (cs, jg, lvl, gold) in pids.items()}}


def _ev(t, ts, pid, **kw):
    return {"type": t, "timestamp": ts, "participantId": pid, **kw}


def _timeline_events(events, me_pid=1):
    return {"info": {
        "participants": [{"participantId": me_pid, "puuid": "me"}],
        "frames": [{"timestamp": 0, "participantFrames": {}, "events": events}],
    }}


def test_parse_starting_items_reads_opening_buy_with_undo():
    tl = _timeline_events([
        _ev("ITEM_PURCHASED", 0, 1, itemId=1055),      # Doran's Blade
        _ev("ITEM_PURCHASED", 1000, 1, itemId=2003),   # Health Potion
        _ev("ITEM_PURCHASED", 1500, 1, itemId=9999),   # mis-buy
        _ev("ITEM_UNDO", 1600, 1, beforeId=9999),      # undone
        _ev("ITEM_PURCHASED", 2000, 6, itemId=1054),   # someone else — ignored
        _ev("ITEM_PURCHASED", 95_000, 1, itemId=3006), # after first back — excluded
    ])
    assert parse_starting_items(tl, "me") == [1055, 2003]


def test_parse_build_order_sorts_final_items_by_purchase_time():
    tl = _timeline_events([
        _ev("ITEM_PURCHASED", 5_000, 1, itemId=1055),    # Doran's — not in final, ignored
        _ev("ITEM_PURCHASED", 500_000, 1, itemId=3111),  # Mercury's (final) — bought 2nd
        _ev("ITEM_PURCHASED", 300_000, 1, itemId=3074),  # Ravenous Hydra (final) — 1st
        _ev("ITEM_PURCHASED", 900_000, 1, itemId=3053),  # Sterak's (final) — 3rd
        _ev("ITEM_PURCHASED", 250_000, 6, itemId=3074),  # other player — ignored
    ])
    final = [3074, 3111, 3053, 0, 0, 0, 3340]  # includes empties + a trinket
    assert parse_build_order(tl, "me", final) == [3074, 3111, 3053]  # purchase order, trinket dropped


def test_parse_build_order_none_without_timeline():
    assert parse_build_order(None, "me", [3074]) is None


def test_parse_skill_order_by_max_priority():
    evs = [
        _ev("SKILL_LEVEL_UP", 1000, 1, skillSlot=1),   # lvl1-3: one point each
        _ev("SKILL_LEVEL_UP", 2000, 1, skillSlot=2),
        _ev("SKILL_LEVEL_UP", 3000, 1, skillSlot=3),
        _ev("SKILL_LEVEL_UP", 200_000, 1, skillSlot=1),  # Q -> 2nd
        _ev("SKILL_LEVEL_UP", 300_000, 1, skillSlot=1),  # Q -> 3rd (maxed first)
        _ev("SKILL_LEVEL_UP", 350_000, 1, skillSlot=4),  # R — ignored
        _ev("SKILL_LEVEL_UP", 400_000, 1, skillSlot=2),  # W -> 2nd
        _ev("SKILL_LEVEL_UP", 500_000, 1, skillSlot=2),  # W -> 3rd
        _ev("SKILL_LEVEL_UP", 250_000, 6, skillSlot=3),  # other player — ignored
    ]
    assert parse_skill_order(_timeline_events(evs), "me") == [1, 2, 3]  # Q > W > E


def test_parse_skill_order_none_without_timeline():
    assert parse_skill_order(None, "me") is None
    assert parse_skill_order(_timeline_events([]), "me") == []


def test_parse_starting_items_none_without_timeline_or_participant():
    assert parse_starting_items(None, "me") is None
    assert parse_starting_items(_timeline_events([]), "ghost") is None
    assert parse_starting_items(_timeline_events([]), "me") == []


def test_parse_timeline_deltas_computes_advantage_vs_opponent():
    tl = _timeline(frames=[
        _frame(0, {1: (0, 0, 1, 500), 6: (0, 0, 1, 500)}),
        _frame(420_000, {1: (55, 4, 6, 2600), 6: (40, 0, 5, 2100)}),   # ~7 min
        _frame(840_000, {1: (120, 8, 10, 5200), 6: (95, 0, 9, 4300)}),  # ~14 min
    ])
    d = parse_timeline_deltas(tl, "me", "opp")
    assert d["cs_diff_7"] == (55 + 4) - 40      # 19
    assert d["level_diff_7"] == 1
    assert d["gold_diff_7"] == 500
    assert d["cs_diff_14"] == (120 + 8) - 95    # 33
    assert d["gold_diff_14"] == 900


def test_parse_timeline_deltas_none_when_no_opponent_or_no_timeline():
    tl = _timeline(frames=[_frame(420_000, {1: (10, 0, 4, 900), 6: (5, 0, 3, 700)})])
    assert all(v is None for v in parse_timeline_deltas(tl, "me", None).values())
    assert all(v is None for v in parse_timeline_deltas(None, "me", "opp").values())


def test_parse_timeline_deltas_short_game_leaves_14m_none():
    # only an early frame — nothing within tolerance of the 14-min mark
    tl = _timeline(frames=[
        _frame(0, {1: (0, 0, 1, 500), 6: (0, 0, 1, 500)}),
        _frame(420_000, {1: (50, 0, 6, 2500), 6: (45, 0, 6, 2400)}),
    ])
    d = parse_timeline_deltas(tl, "me", "opp")
    assert d["cs_diff_7"] == 5
    assert d["cs_diff_14"] is None
    assert d["gold_diff_14"] is None


def test_parse_death_events_extracts_positions_for_victim():
    evs = [
        _ev("CHAMPION_KILL", 65_000, 6, victimId=1, killerId=6, position={"x": 1200, "y": 3400}),
        # my kill of the opponent — not my death, must be skipped
        _ev("CHAMPION_KILL", 500_000, 1, victimId=6, killerId=1, position={"x": 9000, "y": 9000}),
        _ev("CHAMPION_KILL", 900_000, 6, victimId=1, killerId=6, position={"x": 8000, "y": 2000}),
    ]
    deaths = parse_death_events(_timeline_events(evs), "me")
    assert deaths == [
        {"x": 1200, "y": 3400, "timestamp_ms": 65_000},
        {"x": 8000, "y": 2000, "timestamp_ms": 900_000},
    ]


def test_parse_death_events_skips_events_missing_position():
    # WARD_PLACED-shaped/kill events without a position field must be skipped,
    # not crash — mirrors match-v5's real behavior for WARD_PLACED (no
    # position at all) in case a malformed/older event ever lacks one.
    evs = [_ev("CHAMPION_KILL", 65_000, 6, victimId=1, killerId=6)]
    assert parse_death_events(_timeline_events(evs), "me") == []


def test_parse_death_events_none_without_timeline_or_participant():
    assert parse_death_events(None, "me") is None
    assert parse_death_events(_timeline_events([]), "ghost") is None
    assert parse_death_events(_timeline_events([]), "me") == []


def test_parse_metrics_without_challenges_gives_nulls_for_challenge_fields():
    values = parse_metrics(sample_match(challenges=False), "p1")
    assert values["has_challenges"] == 0
    assert values["cs_at_10"] is None
    assert values["self_mitigated"] == 25320  # participant fields still present


def test_parse_metrics_unknown_puuid_returns_none():
    assert parse_metrics(sample_match(), "other") is None


# ---------- jungle start side (strong side / weak side) ----------

# blue team's jungle: blue buff top half, red buff bot half; red team's is the
# 180° rotation of that (their blue buff sits in the bot half)
BLUE_TEAM_BLUE_BUFF = (3800, 7900)    # top half
BLUE_TEAM_RED_BUFF = (7800, 4000)     # bot half
RED_TEAM_BLUE_BUFF = (11000, 6900)    # bot half
RED_TEAM_RED_BUFF = (7100, 10900)     # top half


def _jg_frame(ts, positions, cleared=()):
    """positions: {participantId: (x, y)}; cleared: pids with a camp done."""
    return {"timestamp": ts, "participantFrames": {
        str(pid): {"position": {"x": x, "y": y},
                   "jungleMinionsKilled": 4 if pid in cleared else 0}
        for pid, (x, y) in positions.items()}}


def _jg_timeline(frames, pids=(("blue-jg", 2), ("red-jg", 7))):
    return {"info": {
        "participants": [{"participantId": pid, "puuid": puuid} for puuid, pid in pids],
        "frames": frames}}


def test_map_half_splits_on_the_mid_diagonal():
    assert map_half(*BLUE_TEAM_BLUE_BUFF) == "top"
    assert map_half(*BLUE_TEAM_RED_BUFF) == "bot"
    assert map_half(*RED_TEAM_BLUE_BUFF) == "bot"
    assert map_half(*RED_TEAM_RED_BUFF) == "top"
    assert map_half(5000, 5000) == "bot"  # on the diagonal: resolved, not raised


def test_parse_jungle_starts_reads_both_teams_first_camp():
    tl = _jg_timeline([
        _jg_frame(0, {2: (400, 400), 7: (14400, 14400)}),          # fountains
        _jg_frame(60_000, {2: BLUE_TEAM_BLUE_BUFF, 7: RED_TEAM_BLUE_BUFF},
                  cleared=(2, 7)),
    ])
    assert parse_jungle_starts(tl, {100: "blue-jg", 200: "red-jg"}) == {
        100: "top", 200: "bot"}


def test_parse_jungle_starts_ignores_the_fountain_frame():
    # frame 0 puts everyone on/near the diagonal — using it would misread the
    # blue jungler as "bot" despite a top-half start
    tl = _jg_timeline([
        _jg_frame(0, {2: (400, 400)}),
        _jg_frame(60_000, {2: BLUE_TEAM_BLUE_BUFF}, cleared=(2,)),
    ])
    assert parse_jungle_starts(tl, {100: "blue-jg"}) == {100: "top"}


def test_parse_jungle_starts_falls_back_to_the_60s_frame_when_no_camp_cleared():
    # slow start / leash: no camp done yet, but they're standing at one
    tl = _jg_timeline([_jg_frame(60_000, {2: BLUE_TEAM_RED_BUFF})])
    assert parse_jungle_starts(tl, {100: "blue-jg"}) == {100: "bot"}


def test_parse_jungle_starts_prefers_the_cleared_camp_over_an_early_position():
    # at 60s they're pathing through the bot half having cleared nothing; the
    # first camp they actually finish is top-side
    tl = _jg_timeline([
        _jg_frame(60_000, {2: BLUE_TEAM_RED_BUFF}),
        _jg_frame(120_000, {2: BLUE_TEAM_BLUE_BUFF}, cleared=(2,)),
    ])
    assert parse_jungle_starts(tl, {100: "blue-jg"}) == {100: "top"}


def test_parse_jungle_starts_unknown_when_jungler_or_timeline_missing():
    tl = _jg_timeline([_jg_frame(60_000, {2: BLUE_TEAM_BLUE_BUFF}, cleared=(2,))])
    assert parse_jungle_starts(tl, {100: "blue-jg", 200: "nobody"}) == {
        100: "top", 200: None}
    assert parse_jungle_starts(None, {100: "blue-jg"}) == {100: None}
    assert parse_jungle_starts({"info": {"frames": []}}, {100: "blue-jg"}) == {100: None}


def test_parse_jungle_starts_ignores_frames_past_the_first_clear():
    # by 4 min the jungler has crossed the map — must not count as the start
    tl = _jg_timeline([_jg_frame(240_000, {2: BLUE_TEAM_RED_BUFF}, cleared=(2,))])
    assert parse_jungle_starts(tl, {100: "blue-jg"}) == {100: None}


def test_strongside_is_the_lane_opposite_the_jungle_start():
    # the user's worked example: red team top laner, their jungler starts blue
    # buff (bot half for red) -> opposite the lane -> strong side
    assert strongside("TOP", "bot") is True
    assert strongside("TOP", "top") is False
    assert strongside("BOTTOM", "top") is True
    assert strongside("UTILITY", "top") is True   # support shares the bot lane
    assert strongside("BOTTOM", "bot") is False


def test_strongside_undefined_for_mid_jungle_and_unknown_starts():
    assert strongside("MIDDLE", "top") is None
    assert strongside("JUNGLE", "top") is None
    assert strongside("", "top") is None
    assert strongside("TOP", None) is None
