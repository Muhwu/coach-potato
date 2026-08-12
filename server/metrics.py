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


# Every positioned timeline event we store. 'death' came first (the Trends
# heatmap); the rest were added for VOD chapters, which want the same beats
# Ascent shows. Kept in one place so the db CHECK and the parser can't drift.
MAP_EVENT_TYPES = ("death", "kill", "assist", "tower", "inhibitor", "objective")

# monsterType -> the label a human wants to read in a chapter list
_MONSTER_LABELS = {
    "DRAGON": "Dragon", "RIFTHERALD": "Herald", "BARON_NASHOR": "Baron",
    "HORDE": "Voidgrub", "ATAKHAN": "Atakhan",
}
_TOWER_LABELS = {
    "OUTER_TURRET": "Outer tower", "INNER_TURRET": "Inner tower",
    "BASE_TURRET": "Base tower", "NEXUS_TURRET": "Nexus tower",
}


def _team_of(participant_id):
    """Timeline participants 1-5 are team 100, 6-10 are team 200. The timeline's
    own participant list carries only {participantId, puuid}, so team has to be
    derived — this mapping is fixed for Summoner's Rift."""
    return 100 if participant_id <= 5 else 200


def parse_map_events(timeline_json, puuid):
    """Every positioned event of interest for `puuid`, from the match-v5
    timeline: their kills/deaths/assists, plus towers, inhibitors and epic
    monsters taken by either team.

    Returns [{event_type, x, y, timestamp_ms, detail}] ordered by timestamp, or
    None without a timeline/participant. `detail` is a short human label
    ("Dragon", "Outer tower") prefixed with "-" when the event went against the
    player's team, so chapters can read "Dragon" vs "-Dragon" (lost).

    Only events Riot gives a `position` for are stored, since the same rows
    feed the Trends death heatmap. In practice CHAMPION_KILL, BUILDING_KILL and
    ELITE_MONSTER_KILL all carry one; WARD_PLACED notably does not (see
    CLAUDE.md).
    """
    if not timeline_json:
        return None
    info = timeline_json.get("info") or {}
    pid = next((p.get("participantId") for p in info.get("participants") or []
                if p.get("puuid") == puuid), None)
    if pid is None:
        return None
    my_team = _team_of(pid)
    out = []

    def add(kind, ev, detail=""):
        pos = ev.get("position") or {}
        if "x" not in pos or "y" not in pos:
            return
        out.append({"event_type": kind, "x": pos["x"], "y": pos["y"],
                    "timestamp_ms": ev.get("timestamp") or 0, "detail": detail})

    for frame in info.get("frames") or []:
        for ev in frame.get("events") or []:
            kind = ev.get("type")
            if kind == "CHAMPION_KILL":
                if ev.get("victimId") == pid:
                    add("death", ev)
                elif ev.get("killerId") == pid:
                    add("kill", ev)
                elif pid in (ev.get("assistingParticipantIds") or []):
                    add("assist", ev)
            elif kind == "BUILDING_KILL":
                # teamId on a BUILDING_KILL is the team that LOST the building
                lost_by_us = ev.get("teamId") == my_team
                sign = "-" if lost_by_us else ""
                if ev.get("buildingType") == "TOWER_BUILDING":
                    label = _TOWER_LABELS.get(ev.get("towerType"), "Tower")
                    add("tower", ev, f"{sign}{label}")
                elif ev.get("buildingType") == "INHIBITOR_BUILDING":
                    add("inhibitor", ev, f"{sign}Inhibitor")
            elif kind == "ELITE_MONSTER_KILL":
                label = _MONSTER_LABELS.get(ev.get("monsterType"), "Objective")
                if ev.get("monsterType") == "DRAGON" and ev.get("monsterSubType"):
                    # SUB_TYPE looks like "FIRE_DRAGON" -> "Fire dragon"
                    sub = ev["monsterSubType"].replace("_DRAGON", "").replace("_", " ")
                    label = f"{sub.capitalize()} dragon"
                sign = "" if ev.get("killerTeamId") == my_team else "-"
                add("objective", ev, f"{sign}{label}")

    out.sort(key=lambda e: e["timestamp_ms"])
    return out


def parse_death_events(timeline_json, puuid):
    """Positions where `puuid` died, from the match-v5 timeline's CHAMPION_KILL
    events (`victimId` matched against the participant id resolved the same way
    as parse_timeline_deltas). Each event's `position: {x, y}` is populated by
    Riot for kill events. Returns a list of {x, y, timestamp_ms} dicts ordered
    by timestamp, or None without a timeline/participant.

    NOTE: match-v5 WARD_PLACED events do NOT carry a position field (confirmed
    against a live timeline fetch + Riot's own developer-relations tracker,
    which has an open, unresolved feature request asking for one — see
    CLAUDE.md). Only deaths are extracted here; there is no ward-position
    counterpart."""
    if not timeline_json:
        return None
    info = timeline_json.get("info") or {}
    pid = next((p.get("participantId") for p in info.get("participants") or []
                if p.get("puuid") == puuid), None)
    if pid is None:
        return None
    deaths = []
    for frame in info.get("frames") or []:
        for ev in frame.get("events") or []:
            if ev.get("type") != "CHAMPION_KILL" or ev.get("victimId") != pid:
                continue
            pos = ev.get("position") or {}
            if "x" not in pos or "y" not in pos:
                continue
            deaths.append({"x": pos["x"], "y": pos["y"],
                            "timestamp_ms": ev.get("timestamp") or 0})
    return deaths


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


# ---------- jungle start side (strong side / weak side) ----------
#
# Summoner's Rift splits along the mid-lane diagonal into a top half (top lane
# and both teams' top-side jungles) and a bot half. The halves are ABSOLUTE:
# top lane sits in the top half for blue and red alike, so a team's jungle
# start half can be compared directly against a lane half regardless of side.
# You are "strong side" when your jungler starts in the half OPPOSITE your
# lane — a full clear starting at the far end arrives at your lane first.
JUNGLE_START_FRAME_MS = 60_000     # first frame after the fountain
JUNGLE_START_MAX_MS = 180_000      # give up past ~3 min (first clear is over)
# mid/jungle deliberately absent — they have no strong/weak side under this rule
LANE_HALVES = {"TOP": "top", "BOTTOM": "bot", "UTILITY": "bot"}


def map_half(x, y):
    """Which half of the map a position is in: 'top' above the mid diagonal,
    'bot' below it. Positions exactly on the diagonal (the blue fountain at
    game start, for one) are ambiguous — callers avoid frame 0 for that
    reason; ties resolve to 'bot' rather than raising."""
    return "top" if y > x else "bot"


def _jungle_start_half(frames, pid):
    """The half a jungler started in: the position at the earliest frame where
    they have cleared a camp (jungleMinionsKilled >= 1), else the ~60s frame.
    Preferring the first cleared camp rides out slow starts and leashes, and
    keeps an invading jungler anchored to the camps they actually took."""
    fallback = None
    for frame in frames:
        ts = frame.get("timestamp") or 0
        if ts <= 0 or ts > JUNGLE_START_MAX_MS:
            continue  # frame 0 is the fountain — on the diagonal, meaningless
        pf = (frame.get("participantFrames") or {}).get(str(pid))
        position = (pf or {}).get("position") or {}
        x, y = position.get("x"), position.get("y")
        if x is None or y is None:
            continue
        if fallback is None and ts <= JUNGLE_START_FRAME_MS + FRAME_TOLERANCE_MS:
            fallback = map_half(x, y)
        if (pf.get("jungleMinionsKilled") or 0) >= 1:
            return map_half(x, y)
    return fallback


def parse_jungle_starts(timeline_json, jungler_puuids):
    """Which map half each team's jungler started in, from the match timeline:
    {team_id: 'top' | 'bot' | None}. `jungler_puuids` maps team_id (100/200)
    to that team's JUNGLE player's puuid — derived from stored participants,
    since the timeline itself carries no roles. A team with no identified
    jungler (or no usable frame) is None rather than guessed."""
    out = {team: None for team in jungler_puuids}
    if not timeline_json:
        return out
    info = timeline_json.get("info") or {}
    pid_by_puuid = {p.get("puuid"): p.get("participantId")
                    for p in info.get("participants") or []}
    frames = info.get("frames") or []
    if not frames:
        return out
    for team, puuid in jungler_puuids.items():
        pid = pid_by_puuid.get(puuid)
        if pid is not None:
            out[team] = _jungle_start_half(frames, pid)
    return out


def strongside(lane_position, lane_jungle_half):
    """Whether a player in `lane_position` is strong side, given the half their
    OWN team's jungler started in. None when it doesn't apply: mid and jungle
    have no strong/weak side under this rule, and an unknown jungle start
    can't be graded."""
    lane_half = LANE_HALVES.get(lane_position)
    if lane_half is None or lane_jungle_half is None:
        return None
    return lane_jungle_half != lane_half
def parse_frame_series(timeline_json):
    """Full-game per-minute gold/CS/XP/level series for EVERY participant in
    a match timeline (not just two marks like parse_timeline_deltas) — the
    source for the full-game curve chart. Returns {puuid: [{"minute", "cs",
    "xp", "gold", "level"}, ...]}, one entry per timeline frame, ordered as
    the frames appear. Empty dict without a timeline. minute = round(frame
    timestamp-ms / 60000)."""
    if not timeline_json:
        return {}
    info = timeline_json.get("info") or {}
    pid_to_puuid = {p.get("participantId"): p.get("puuid")
                    for p in info.get("participants") or []}
    out = {}
    for frame in info.get("frames") or []:
        minute = round((frame.get("timestamp") or 0) / 60_000)
        for pid_str, pf in (frame.get("participantFrames") or {}).items():
            puuid = pid_to_puuid.get(int(pid_str))
            if not puuid:
                continue
            out.setdefault(puuid, []).append({
                "minute": minute, "cs": _cs(pf), "xp": pf.get("xp"),
                "gold": pf.get("totalGold"), "level": pf.get("level"),
            })
    return out
