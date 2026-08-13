"""Recover in-game events from Ascent's log files.

Ascent records through Overwolf's Game Event Provider, which reads League's
**Live Client Data** feed — the same feed the in-game client exposes. Every
event it sees is written into Ascent's rolling log at
`%LOCALAPPDATA%\\Ascent\\logs\\*.log`, embedded in periodic game snapshots:

    [GEP] event=MatchStarted game_id=1 match_id="EUW1_7934317254"
    ... {"allPlayers":[...],"events":[{"EventID":2,"EventName":"ChampionKill",
        "EventTime":45.43,"KillerName":{"Summoner":"Vinz"},
        "VictimName":"JoyVoid","Assisters":[...]}, ...]}

Why this exists: the same kills/towers/objectives are in Riot's match timeline,
but fetching that needs a working API key (development keys expire daily). The
logs need nothing — so recent games get their VOD chapters filled in offline.

Two things it deliberately cannot do:

  * **No positions.** Live Client Data carries no coordinates, so these events
    give timings only and never appear on the map. Rows are stored with
    `source='ascent_log'` and NULL x/y; a later timeline import supersedes them.
  * **Only recent games.** Logs roll over, so this reaches back days, not
    months. It complements the timeline backfill rather than replacing it.

`EventTime` is seconds from game start, which is what the VOD chapter list
wants — the same clock the timeline's `timestamp_ms` uses.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# `match_id="EUW1_123"` from the GEP lines that bracket each game
MATCH_ID_RE = re.compile(r'match_id="([^"]+)"')
EVENTS_KEY = '"events":['
# the same snapshot carries the scoreboard, which is the only place team
# membership appears — without it every objective a teammate took would read
# as one we lost
PLAYERS_KEY = '"allPlayers":['

# Live Client Data event name -> (our event_type, label). Kill/death/assist are
# decided per-event from who did what, so ChampionKill maps to None here.
EVENT_KINDS = {
    "TurretKilled": ("tower", "Tower"),
    "InhibKilled": ("inhibitor", "Inhibitor"),
    "DragonKill": ("objective", "Dragon"),
    "BaronKill": ("objective", "Baron"),
    "HeraldKill": ("objective", "Herald"),
    "HordeKill": ("objective", "Voidgrub"),
}


def default_log_dir():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path = Path(local) / "Ascent" / "logs"
    return path if path.is_dir() else None


def _extract_json_array(text, start):
    """The JSON array beginning at `start` ('['), or None if it never closes.
    Hand-scanned rather than regexed because these arrays nest objects."""
    depth = 0
    for i in range(start, len(text)):
        char = text[i]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


def _summoner(value):
    """KillerName is sometimes {"Summoner": "Name"} and sometimes a bare
    string, depending on the event and Ascent's version."""
    if isinstance(value, dict):
        return value.get("Summoner") or value.get("summoner") or ""
    return value or ""


def _base_name(name):
    """"Atsuya Fubuki#0510" -> "atsuya fubuki#0510"; tolerant of a missing tag
    so a bare in-game name still matches a configured Riot ID."""
    return (name or "").strip().lower()


def read_log_events(log_dir):
    """-> {match_id: {"events": [...], "players": [...]}}, best per match.

    A game is snapshotted repeatedly as it goes, so the longest event list seen
    for a match is the most complete one. The scoreboard (`allPlayers`) from
    that same snapshot comes with it, since team membership is what decides
    whether an objective was taken or lost.
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"no Ascent log directory at {directory}")
    best = {}
    for path in sorted(directory.glob("*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # positions of every match id, so a snapshot can be attributed to the
        # game that was running when it was written
        marks = [(m.start(), m.group(1)) for m in MATCH_ID_RE.finditer(text)]
        if not marks:
            continue
        index = 0
        for m in re.finditer(re.escape(EVENTS_KEY), text):
            events = _extract_json_array(text, m.end() - 1)
            if not events:
                continue
            while index + 1 < len(marks) and marks[index + 1][0] < m.start():
                index += 1
            if marks[index][0] > m.start():
                continue  # snapshot precedes the first match id
            match_id = marks[index][1]
            if len(events) > len(best.get(match_id, {}).get("events", ())):
                # the scoreboard sits just before the events in the same blob
                players_at = text.rfind(PLAYERS_KEY, 0, m.start())
                players = (_extract_json_array(text, players_at + len(PLAYERS_KEY) - 1)
                           if players_at != -1 else None) or []
                best[match_id] = {"events": events, "players": players}
    return best


def team_lookup(players, my_names):
    """-> (name -> team, our team) from a snapshot scoreboard."""
    mine = {_base_name(n) for n in my_names if n}
    teams = {}
    my_team = None
    for player in players or []:
        name = _base_name(player.get("summonerName"))
        team = player.get("team")
        if not name or not team:
            continue
        teams[name] = team
        if name in mine:
            my_team = team
    return teams, my_team


def to_map_events(raw_events, my_names, players=None):
    """Live Client events -> the {event_type, timestamp_ms, detail} shape
    db.replace_map_events stores. `my_names` are the tracked account's names
    (Riot ID and/or in-game name), used to tell our kills from everyone's.

    Events involving nobody we track are still kept when they are objectives or
    structures — "Baron at 24:10" is worth a chapter regardless of who took it —
    but champion kills we had no part in are dropped, or every chapter list
    would be 40 entries of other people's fights.
    """
    mine = {_base_name(n) for n in my_names if n}
    teams, my_team = team_lookup(players, my_names)
    out = []
    for event in raw_events:
        name = event.get("EventName")
        time_s = event.get("EventTime")
        if name is None or time_s is None:
            continue
        ms = int(float(time_s) * 1000)

        if name == "ChampionKill":
            killer = _base_name(_summoner(event.get("KillerName")))
            victim = _base_name(event.get("VictimName"))
            assisters = {_base_name(_summoner(a)) for a in event.get("Assisters") or []}
            if victim in mine:
                kind, detail = "death", ""
            elif killer in mine:
                kind, detail = "kill", ""
            elif mine & assisters:
                kind, detail = "assist", ""
            else:
                continue  # someone else's fight
        elif name in EVENT_KINDS:
            kind, label = EVENT_KINDS[name]
            if name == "DragonKill" and event.get("DragonType"):
                label = f"{event['DragonType']} dragon"
            killer = _base_name(_summoner(event.get("KillerName")))
            # "-" marks an event that went against us — decided by the killer's
            # TEAM, so a tower a teammate took still reads as ours. With no
            # scoreboard to go on, fall back to "ours" rather than labelling
            # everything a loss.
            if my_team and killer in teams:
                ours = teams[killer] == my_team
            else:
                ours = killer in mine or not my_team
            detail = label if ours else f"-{label}"
        else:
            continue  # FirstBlood/Multikill/Ace duplicate a ChampionKill

        out.append({"event_type": kind, "timestamp_ms": ms, "detail": detail,
                    "x": None, "y": None})
    out.sort(key=lambda e: e["timestamp_ms"])
    return out


def sync(conn, log_dir, accounts, only_match_ids=None):
    """Store log-derived events for every crawled game we can find them for.

    `accounts` are the tracked Riot IDs ("Name#TAG"), kept for callers/back-
    compat; identity is resolved per participant from the stored match. Returns
    {"matches_in_logs", "imported", "events", "skipped_no_participant"}.
    """
    from server import db  # local import: db knows nothing about us

    by_match = read_log_events(log_dir)
    result = {"matches_in_logs": len(by_match), "imported": 0, "events": 0,
              "skipped_no_participant": 0}
    for match_id, raw in by_match.items():
        if only_match_ids is not None and match_id not in only_match_ids:
            continue
        rows = conn.execute(
            """SELECT p.puuid, p.champion_name, pl.game_name, pl.tag_line
               FROM participants p JOIN players pl ON pl.puuid = p.puuid
               WHERE p.match_id = ? AND pl.is_tracked = 1""", (match_id,)).fetchall()
        if not rows:
            result["skipped_no_participant"] += 1
            continue
        for row in rows:
            # Which identity the log uses is NOT consistent: most snapshots
            # carry the Riot ID, but some carry the CHAMPION name instead
            # (seen in real logs). Match on both — a champion is unique within
            # a game, so it cannot collide. Deliberately not the other tracked
            # accounts: in a game where both played, their kills are not this
            # player's.
            names = [f"{row['game_name']}#{row['tag_line']}", row["game_name"],
                     row["champion_name"]]
            events = to_map_events(raw["events"], names, raw.get("players"))
            if not events:
                continue
            db.replace_map_events(conn, match_id, row["puuid"], events,
                                  source="ascent_log")
            result["imported"] += 1
            result["events"] += len(events)
    return result
