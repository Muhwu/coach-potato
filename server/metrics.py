"""Registry of coaching metrics extracted from match-v5 payloads.

The registry drives the participant_metrics DDL, payload parsing, SQL
aggregation and the metadata the frontend uses for labels/grouping/deltas.

agg kinds:
  avg      — AVG(col)
  pct01    — 100 * AVG(col)          (0..1 ratios and 0/1 flags)
  per_min  — 60 * SUM(col) / SUM(duration of rows where col present)
  pct_time — 100 * SUM(col) / SUM(duration of rows where col present)

direction: 1 = higher is better, -1 = lower is better, 0 = neutral.
"""


def _metric(key, label, group, field, source="challenges", agg="avg",
            direction=1, decimals=1, suffix="", default_hidden=False, signed=False):
    return {"key": key, "label": label, "group": group, "field": field,
            "source": source, "agg": agg, "direction": direction,
            "decimals": decimals, "suffix": suffix,
            # default_hidden: off in each view's column picker until ticked.
            # signed: show a leading + for positive values (deltas).
            "default_hidden": default_hidden, "signed": signed}


METRICS = [
    # --- Laning ---
    _metric("cs_at_10", "CS @ 10 min", "Laning", "laneMinionsFirst10Minutes"),
    _metric("lane_adv_early", "Ahead in lane @ ~7 min", "Laning",
            "earlyLaningPhaseGoldExpAdvantage", agg="pct01", decimals=0, suffix="%"),
    _metric("lane_adv_late", "Ahead in lane @ ~14 min", "Laning",
            "laningPhaseGoldExpAdvantage", agg="pct01", decimals=0, suffix="%"),
    _metric("max_cs_lead", "Max CS lead on opponent", "Laning",
            "maxCsAdvantageOnLaneOpponent"),
    _metric("max_level_lead", "Max level lead on opponent", "Laning",
            "maxLevelLeadLaneOpponent", decimals=2),
    _metric("plates", "Turret plates taken", "Laning", "turretPlatesTaken", decimals=2),
    _metric("solo_kills", "Solo kills", "Laning", "soloKills", decimals=2),
    _metric("early_takedowns", "Takedowns before ~15 min", "Laning",
            "takedownsFirstXMinutes", decimals=2),
    # --- Lane deltas vs the direct lane opponent, from the match timeline
    # (source="timeline"): my value minus theirs at the frame nearest 7/14
    # min. Hidden by default; None when there's no lane opponent or the game
    # ended before the mark. See metrics.parse_timeline_deltas / crawler. ---
    _metric("cs_diff_7", "ΔCS (7m)", "Laning", "cs_diff_7", source="timeline",
            decimals=1, default_hidden=True, signed=True),
    _metric("level_diff_7", "ΔLevel (7m)", "Laning", "level_diff_7", source="timeline",
            decimals=2, default_hidden=True, signed=True),
    _metric("gold_diff_7", "ΔGold (7m)", "Laning", "gold_diff_7", source="timeline",
            decimals=0, default_hidden=True, signed=True),
    _metric("cs_diff_14", "ΔCS (14m)", "Laning", "cs_diff_14", source="timeline",
            decimals=1, default_hidden=True, signed=True),
    _metric("level_diff_14", "ΔLevel (14m)", "Laning", "level_diff_14", source="timeline",
            decimals=2, default_hidden=True, signed=True),
    _metric("gold_diff_14", "ΔGold (14m)", "Laning", "gold_diff_14", source="timeline",
            decimals=0, default_hidden=True, signed=True),
    # --- Damage & fighting ---
    _metric("team_dmg_pct", "Share of team's damage", "Damage & fighting",
            "teamDamagePercentage", agg="pct01", suffix="%"),
    _metric("kill_participation", "Kill participation", "Damage & fighting",
            "killParticipation", agg="pct01", decimals=0, suffix="%"),
    _metric("dmg_taken_team_pct", "Share of team's damage taken", "Damage & fighting",
            "damageTakenOnTeamPercentage", agg="pct01", direction=0, suffix="%"),
    _metric("skillshots_dodged", "Skillshots dodged", "Damage & fighting",
            "skillshotsDodged"),
    _metric("self_mitigated", "Damage self-mitigated / min", "Damage & fighting",
            "damageSelfMitigated", source="participant", agg="per_min", decimals=0),
    # --- Objectives & map ---
    _metric("turret_takedowns", "Turret takedowns", "Objectives & map",
            "turretTakedowns", decimals=2),
    _metric("turret_damage", "Damage to turrets", "Objectives & map",
            "damageDealtToTurrets", source="participant", decimals=0),
    _metric("tp_takedowns", "Teleport takedowns", "Objectives & map",
            "teleportTakedowns", decimals=2),
    _metric("herald_takedowns", "Rift Herald takedowns", "Objectives & map",
            "riftHeraldTakedowns", decimals=2),
    # --- Vision & survival ---
    _metric("vision_per_min", "Vision score / min", "Vision & survival",
            "visionScorePerMinute", decimals=2),
    _metric("vision_adv", "Vision advantage vs opponent", "Vision & survival",
            "visionScoreAdvantageLaneOpponent", decimals=2),
    _metric("control_wards", "Control wards placed", "Vision & survival",
            "controlWardsPlaced", decimals=2),
    _metric("ward_takedowns", "Ward takedowns", "Vision & survival",
            "wardTakedowns", decimals=2),
    _metric("time_dead", "Time dead (% of game)", "Vision & survival",
            "totalTimeSpentDead", source="participant", agg="pct_time",
            direction=-1, suffix="%"),
]

GROUPS = ["Laning", "Damage & fighting", "Objectives & map", "Vision & survival"]


def metric_keys():
    return [m["key"] for m in METRICS]


def parse_metrics(match_json, puuid):
    """Extract raw metric values for one participant. None if puuid absent."""
    participant = next(
        (p for p in match_json["info"]["participants"] if p["puuid"] == puuid), None)
    if participant is None:
        return None
    challenges = participant.get("challenges") or {}
    values = {"has_challenges": int(bool(challenges))}
    for m in METRICS:
        if m["source"] == "timeline":
            values[m["key"]] = None  # filled separately from the match timeline
        elif m["source"] == "participant":
            values[m["key"]] = participant.get(m["field"])
        else:
            values[m["key"]] = challenges.get(m["field"])
    return values


# frame timestamps (ms) we sample the timeline at; a frame must land within
# FRAME_TOLERANCE_MS of the mark (games that ended earlier yield None)
LANE_DELTA_MARKS = {7: 420_000, 14: 840_000}
FRAME_TOLERANCE_MS = 90_000
TIMELINE_KEYS = [m["key"] for m in METRICS if m["source"] == "timeline"]


def _frame_near(frames, target_ms):
    """Frame whose timestamp is closest to target_ms, or None if none is
    within FRAME_TOLERANCE_MS (e.g. the game ended before the mark)."""
    best, best_gap = None, None
    for f in frames:
        gap = abs(f.get("timestamp", 0) - target_ms)
        if best_gap is None or gap < best_gap:
            best, best_gap = f, gap
    if best is None or best_gap > FRAME_TOLERANCE_MS:
        return None
    return best


def _cs(pf):
    return (pf.get("minionsKilled") or 0) + (pf.get("jungleMinionsKilled") or 0)


# the game-start shopping trip happens during loading (events at ~t0); first
# recall is well after minions (~90s), so a 30s window captures the opening buy
# and nothing from a later back.
STARTING_ITEMS_BEFORE_MS = 30_000


def parse_starting_items(timeline_json, puuid, before_ms=STARTING_ITEMS_BEFORE_MS):
    """The items a player bought at game start, from the match-v5 timeline's
    ITEM_PURCHASED events before `before_ms` (with ITEM_UNDO applied). Returns a
    list of item ids (order bought), or None if the timeline/participant is
    missing. Empty list means a timeline was present but no early purchase."""
    if not timeline_json:
        return None
    info = timeline_json.get("info") or {}
    pid = next((p.get("participantId") for p in info.get("participants") or []
                if p.get("puuid") == puuid), None)
    if pid is None:
        return None
    items = []
    for frame in info.get("frames") or []:
        for ev in frame.get("events") or []:
            if ev.get("participantId") != pid or (ev.get("timestamp") or 0) > before_ms:
                continue
            if ev.get("type") == "ITEM_PURCHASED":
                item_id = ev.get("itemId")
                if item_id:
                    items.append(item_id)
            elif ev.get("type") == "ITEM_UNDO":
                before_id = ev.get("beforeId")  # the undone purchase
                if before_id in items:
                    items.remove(before_id)
    return items


# ward trinkets + consumables/wards — never part of the "what did they build"
# order (pots/biscuits/elixirs/control wards can linger in the final inventory)
_TRINKET_IDS = {3340, 3363, 3364}
_NON_BUILD_IDS = _TRINKET_IDS | {2003, 2010, 2022, 2031, 2033, 2055,
                                 2138, 2139, 2140, 2150, 2151, 2152}


def parse_build_order(timeline_json, puuid, final_item_ids):
    """Order a player's FINAL-inventory items by when they were bought/completed
    in the timeline (first ITEM_PURCHASED per item id), so the first entries are
    the first items they built toward — not the arbitrary final-slot order.
    final_item_ids: their item0..item6 ids. Trinkets are dropped. Items with no
    purchase event (rare) are appended in inventory order. Returns an ordered
    list of item ids, or None without a timeline/participant."""
    if not timeline_json:
        return None
    info = timeline_json.get("info") or {}
    pid = next((p.get("participantId") for p in info.get("participants") or []
                if p.get("puuid") == puuid), None)
    if pid is None:
        return None
    final = [i for i in (final_item_ids or []) if i and i not in _NON_BUILD_IDS]
    first_ts = {}
    for frame in info.get("frames") or []:
        for ev in frame.get("events") or []:
            if ev.get("participantId") != pid or ev.get("type") != "ITEM_PURCHASED":
                continue
            iid = ev.get("itemId")
            if iid in final and iid not in first_ts:
                first_ts[iid] = ev.get("timestamp") or 0
    # drop items first bought in the opening window — those are the starting buy
    # (shown separately); Build is the path after it.
    built = {i: t for i, t in first_ts.items() if t >= STARTING_ITEMS_BEFORE_MS}
    ordered = sorted(built, key=built.get)
    for i in final:  # items with no post-start purchase event kept last, inventory order
        if i not in first_ts and i not in ordered:
            ordered.append(i)
    return ordered


def parse_skill_order(timeline_json, puuid):
    """The player's ability MAX order (which of Q/W/E they leveled first), from
    the timeline's SKILL_LEVEL_UP events. Slots: 1=Q, 2=W, 3=E (R/slot 4 is
    ignored — it's always taken at 6/11/16). Ordered by which basic ability
    reached its 3rd point first (the classic 'max priority' signal); abilities
    with fewer points fall back to point count. Returns [slot, slot, slot] or
    None without a timeline/participant."""
    if not timeline_json:
        return None
    info = timeline_json.get("info") or {}
    pid = next((p.get("participantId") for p in info.get("participants") or []
                if p.get("puuid") == puuid), None)
    if pid is None:
        return None
    counts = {1: 0, 2: 0, 3: 0}
    third_ts = {}  # timestamp each basic ability reached its 3rd point
    for frame in info.get("frames") or []:
        for ev in frame.get("events") or []:
            if ev.get("type") != "SKILL_LEVEL_UP" or ev.get("participantId") != pid:
                continue
            slot = ev.get("skillSlot")
            if slot in counts:
                counts[slot] += 1
                if counts[slot] == 3:
                    third_ts[slot] = ev.get("timestamp") or 0
    if not any(counts.values()):
        return []
    # maxed-first (3rd point earliest) wins; otherwise more points, then Q<W<E
    def key(s):
        return (0, third_ts[s]) if s in third_ts else (1, -counts[s], s)
    return sorted((1, 2, 3), key=key)


def parse_timeline_deltas(timeline_json, me_puuid, opp_puuid):
    """CS/level/gold advantage of me_puuid over opp_puuid at ~7 and ~14 min,
    read from the match-v5 timeline. Returns {timeline metric key: value},
    each None when the opponent is unknown or the frame is missing."""
    blank = {k: None for k in TIMELINE_KEYS}
    if not timeline_json or not opp_puuid:
        return blank
    info = timeline_json.get("info") or {}
    pid_by_puuid = {p.get("puuid"): p.get("participantId")
                    for p in info.get("participants") or []}
    me_pid, opp_pid = pid_by_puuid.get(me_puuid), pid_by_puuid.get(opp_puuid)
    frames = info.get("frames") or []
    if me_pid is None or opp_pid is None or not frames:
        return blank
    out = dict(blank)
    for minute, target in LANE_DELTA_MARKS.items():
        frame = _frame_near(frames, target)
        if not frame:
            continue
        pf = frame.get("participantFrames") or {}
        mine, theirs = pf.get(str(me_pid)), pf.get(str(opp_pid))
        if not mine or not theirs:
            continue
        out[f"cs_diff_{minute}"] = _cs(mine) - _cs(theirs)
        out[f"level_diff_{minute}"] = (mine.get("level") or 0) - (theirs.get("level") or 0)
        out[f"gold_diff_{minute}"] = (mine.get("totalGold") or 0) - (theirs.get("totalGold") or 0)
    return out
