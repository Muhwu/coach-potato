"""Ascent VOD import + the recording/YouTube API.

No network and no real Ascent install: a synthetic Ascent database is built in
a tmp_path, and the YouTube client is mocked. What these guard is the two
promises the feature makes — Ascent's database is only ever read, and video
files are never touched — plus the match-id linking that everything hangs off.
"""
import os
import sqlite3
from unittest import mock

import pytest

from server import db, recordings, youtube
from test_stats import add_match  # canonical fixture builder


def make_ascent_db(path, rows):
    """A stand-in for Ascent's own database, with the columns we read."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE recordings (
        uuid TEXT PRIMARY KEY, video_path TEXT, created_at INTEGER,
        duration_s REAL, game_match_id TEXT, unrelated TEXT)""")
    conn.executemany(
        "INSERT INTO recordings VALUES (?, ?, ?, ?, ?, 'ignored')", rows)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "lol.sqlite")
    yield connection
    connection.close()


def test_reads_only_matched_recordings(tmp_path, conn):
    add_match(conn, my_champ="Gwen", when=1_700_000_000_000)
    match_id = conn.execute("SELECT match_id FROM matches").fetchone()["match_id"]
    video = tmp_path / "game.mp4"
    video.write_bytes(b"x")
    ascent = make_ascent_db(tmp_path / "ascent.db", [
        ("u1", str(video), 1_700_000_030_000, 1800.0, match_id),
        ("u2", str(video), 1_700_000_040_000, 1800.0, "EUW1_NOT_CRAWLED"),
    ])

    result = recordings.sync(conn, ascent)
    assert result["imported"] == 1
    assert result["skipped_unmatched"] == 1
    stored = db.recordings_for_match(conn, match_id)
    assert [r["uuid"] for r in stored] == ["u1"]


def test_skips_recordings_whose_file_is_gone(tmp_path, conn):
    add_match(conn, when=1_700_000_000_000)
    match_id = conn.execute("SELECT match_id FROM matches").fetchone()["match_id"]
    ascent = make_ascent_db(tmp_path / "ascent.db", [
        ("u1", str(tmp_path / "deleted.mp4"), 1_700_000_030_000, 60.0, match_id)])
    result = recordings.sync(conn, ascent)
    assert result["imported"] == 0 and result["skipped_missing_file"] == 1
    assert db.recordings_for_match(conn, match_id) == []


def test_sync_is_idempotent_and_keeps_our_own_columns(tmp_path, conn):
    add_match(conn, when=1_700_000_000_000)
    match_id = conn.execute("SELECT match_id FROM matches").fetchone()["match_id"]
    video = tmp_path / "game.mp4"
    video.write_bytes(b"x")
    ascent = make_ascent_db(tmp_path / "ascent.db",
                            [("u1", str(video), 1_700_000_030_000, 1800.0, match_id)])

    recordings.sync(conn, ascent)
    db.set_recording_offset(conn, "u1", -2500)
    db.set_recording_youtube(conn, "u1", "YT1", "private")

    recordings.sync(conn, ascent)  # re-run must not clobber our columns
    row = db.get_recording(conn, "u1")
    assert row["offset_ms"] == -2500
    assert row["youtube_video_id"] == "YT1"
    assert len(db.recordings_for_match(conn, match_id)) == 1


def test_ascent_database_is_never_written(tmp_path, conn):
    """The whole point of snapshotting: Ascent owns that file."""
    add_match(conn, when=1_700_000_000_000)
    match_id = conn.execute("SELECT match_id FROM matches").fetchone()["match_id"]
    video = tmp_path / "game.mp4"
    video.write_bytes(b"x")
    ascent = make_ascent_db(tmp_path / "ascent.db",
                            [("u1", str(video), 1_700_000_030_000, 60.0, match_id)])
    before = (os.path.getmtime(ascent), os.path.getsize(ascent),
              ascent.read_bytes())
    recordings.sync(conn, ascent)
    after = (os.path.getmtime(ascent), os.path.getsize(ascent), ascent.read_bytes())
    assert before == after


def test_forgetting_a_recording_leaves_the_video_alone(tmp_path, conn):
    add_match(conn, when=1_700_000_000_000)
    match_id = conn.execute("SELECT match_id FROM matches").fetchone()["match_id"]
    video = tmp_path / "game.mp4"
    video.write_bytes(b"still here")
    ascent = make_ascent_db(tmp_path / "ascent.db",
                            [("u1", str(video), 1_700_000_030_000, 60.0, match_id)])
    recordings.sync(conn, ascent)
    assert db.delete_recording(conn, "u1") is True
    assert video.exists() and video.read_bytes() == b"still here"


def test_missing_or_foreign_database_is_reported_clearly(tmp_path, conn):
    with pytest.raises(FileNotFoundError):
        recordings.sync(conn, tmp_path / "nope.db")
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE something (a INT)").connection.close()
    with pytest.raises(ValueError, match="recordings"):
        recordings.sync(conn, other)


def test_death_markers_map_timeline_to_video_position(tmp_path, conn):
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute(
        "SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 2, "timestamp_ms": 300_000},
        {"event_type": "death", "x": 3, "y": 4, "timestamp_ms": 900_000},
    ])
    marks = recordings.death_markers(conn, row["match_id"], row["puuid"])
    assert [m["video_ms"] for m in marks] == [300_000, 900_000]
    # a negative offset pulls markers earlier, and never below zero
    shifted = recordings.death_markers(conn, row["match_id"], row["puuid"], -400_000)
    assert [m["video_ms"] for m in shifted] == [0, 500_000]


def _timeline(events, me_pid=1, puuid="me-1"):
    return {"info": {"participants": [{"participantId": me_pid, "puuid": puuid}],
                     "frames": [{"events": events}]}}


def _at(x=100, y=200):
    return {"position": {"x": x, "y": y}}


def test_parses_kills_deaths_and_assists():
    from server import metrics
    events = metrics.parse_map_events(_timeline([
        {"type": "CHAMPION_KILL", "timestamp": 1000, "victimId": 1, **_at()},
        {"type": "CHAMPION_KILL", "timestamp": 2000, "killerId": 1, "victimId": 7, **_at()},
        {"type": "CHAMPION_KILL", "timestamp": 3000, "killerId": 2, "victimId": 8,
         "assistingParticipantIds": [1, 3], **_at()},
        {"type": "CHAMPION_KILL", "timestamp": 4000, "killerId": 6, "victimId": 7, **_at()},
    ]), "me-1")
    assert [e["event_type"] for e in events] == ["death", "kill", "assist"]


def test_parses_towers_and_marks_which_side_lost_them():
    from server import metrics
    events = metrics.parse_map_events(_timeline([
        # participant 1 is team 100, so teamId 200 losing a tower is ours taken
        {"type": "BUILDING_KILL", "timestamp": 1000, "teamId": 200,
         "buildingType": "TOWER_BUILDING", "towerType": "OUTER_TURRET", **_at()},
        {"type": "BUILDING_KILL", "timestamp": 2000, "teamId": 100,
         "buildingType": "TOWER_BUILDING", "towerType": "INNER_TURRET", **_at()},
        {"type": "BUILDING_KILL", "timestamp": 3000, "teamId": 200,
         "buildingType": "INHIBITOR_BUILDING", **_at()},
    ]), "me-1")
    assert [(e["event_type"], e["detail"]) for e in events] == [
        ("tower", "Outer tower"), ("tower", "-Inner tower"), ("inhibitor", "Inhibitor")]


def test_parses_epic_monsters_with_readable_names():
    from server import metrics
    events = metrics.parse_map_events(_timeline([
        {"type": "ELITE_MONSTER_KILL", "timestamp": 1000, "killerTeamId": 100,
         "monsterType": "DRAGON", "monsterSubType": "FIRE_DRAGON", **_at()},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 2000, "killerTeamId": 200,
         "monsterType": "BARON_NASHOR", **_at()},
        {"type": "ELITE_MONSTER_KILL", "timestamp": 3000, "killerTeamId": 100,
         "monsterType": "RIFTHERALD", **_at()},
    ]), "me-1")
    assert [e["detail"] for e in events] == ["Fire dragon", "-Baron", "Herald"]
    assert all(e["event_type"] == "objective" for e in events)


def test_events_without_a_position_are_skipped():
    """The same rows feed the death heatmap, which needs x/y."""
    from server import metrics
    events = metrics.parse_map_events(_timeline([
        {"type": "CHAMPION_KILL", "timestamp": 1000, "victimId": 1},  # no position
        {"type": "WARD_PLACED", "timestamp": 1500, "creatorId": 1},   # never has one
    ]), "me-1")
    assert events == []


def test_chapter_labels_read_like_a_human_wrote_them():
    chapters = recordings.build_chapters([
        {"video_ms": 60_000, "event_type": "kill"},
        {"video_ms": 120_000, "event_type": "death"},
        {"video_ms": 180_000, "event_type": "objective", "detail": "Fire dragon"},
        {"video_ms": 240_000, "event_type": "tower", "detail": "-Outer tower"},
        {"video_ms": 300_000, "event_type": "kill"},
    ])
    assert chapters == [
        (0, "Game start"), (60_000, "Kill 1"), (120_000, "Death 1"),
        (180_000, "Fire dragon"), (240_000, "Lost outer tower"), (300_000, "Kill 2")]


def test_merged_chapters_keep_the_more_notable_label():
    """A kill and the dragon three seconds later are one moment on the video.
    On a personal review VOD what you did outranks the objective around it, so
    the kill is the label that survives."""
    chapters = recordings.build_chapters([
        {"video_ms": 60_000, "event_type": "kill"},
        {"video_ms": 63_000, "event_type": "objective", "detail": "Baron"},
    ])
    assert chapters == [(0, "Game start"), (60_000, "Kill 1")]


def test_a_death_is_never_swallowed_by_a_nearby_objective():
    """Regression: a real game lost all six of its deaths to voidgrub chapters
    that happened seconds earlier."""
    chapters = recordings.build_chapters([
        {"video_ms": 60_000, "event_type": "objective", "detail": "Voidgrub"},
        {"video_ms": 64_000, "event_type": "death"},
    ])
    assert chapters == [(0, "Game start"), (60_000, "Death 1")]


def test_map_keeps_its_dots_even_when_the_log_wins_the_chapters(conn):
    """Regression: choosing the log for chapters (more events) also emptied the
    map, because log events have no coordinates. The map reads positioned rows
    independently of the chapter source."""
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 5, "y": 6, "timestamp_ms": 100, "detail": ""},
    ], source="timeline")
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": None, "y": None, "timestamp_ms": 100, "detail": ""},
        {"event_type": "kill", "x": None, "y": None, "timestamp_ms": 200, "detail": ""},
        {"event_type": "tower", "x": None, "y": None, "timestamp_ms": 300, "detail": "Tower"},
    ], source="ascent_log")

    # chapters come from the richer source...
    assert len(recordings.timeline_markers(conn, row["match_id"], row["puuid"])) == 3
    # ...while the map still gets the one event that has a position
    dots = recordings.positioned_markers(conn, row["match_id"], row["puuid"])
    assert [(d["event_type"], d["x"], d["y"]) for d in dots] == [("death", 5, 6)]


def test_events_from_one_source_only(conn):
    """Both sources describe the same game; reading both would double every
    death. The richer source wins — and a deaths-only timeline is not it."""
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 1, "timestamp_ms": 100, "detail": ""},
    ], source="timeline")
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": None, "y": None, "timestamp_ms": 100, "detail": ""},
        {"event_type": "kill", "x": None, "y": None, "timestamp_ms": 200, "detail": ""},
        {"event_type": "tower", "x": None, "y": None, "timestamp_ms": 300, "detail": "Tower"},
    ], source="ascent_log")

    assert recordings.preferred_source(conn, row["match_id"], row["puuid"]) == "ascent_log"
    marks = recordings.timeline_markers(conn, row["match_id"], row["puuid"])
    assert len(marks) == 3                       # not 4 — no mixing
    assert len(recordings.death_markers(conn, row["match_id"], row["puuid"])) == 1


def test_merging_never_displaces_the_zero_opener():
    chapters = recordings.build_chapters([
        {"video_ms": 2_000, "event_type": "objective", "detail": "Herald"}])
    assert chapters == [(0, "Game start")]


def test_timeline_markers_return_every_type_but_death_markers_do_not(conn):
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 1, "timestamp_ms": 100, "detail": ""},
        {"event_type": "kill", "x": 2, "y": 2, "timestamp_ms": 200, "detail": ""},
        {"event_type": "objective", "x": 3, "y": 3, "timestamp_ms": 300, "detail": "Baron"},
    ])
    everything = recordings.timeline_markers(conn, row["match_id"], row["puuid"])
    assert [e["event_type"] for e in everything] == ["death", "kill", "objective"]
    assert everything[2]["detail"] == "Baron"
    # the ☠ seek buttons under the video stay deaths-only
    assert [e["event_type"] for e in
            recordings.death_markers(conn, row["match_id"], row["puuid"])] == ["death"]


def test_heatmap_still_only_sees_deaths(conn):
    """Widening the table must not make the Trends death map plot kills."""
    from server import stats
    add_match(conn, my_champ="Gwen", when=1_700_000_000_000)
    row = conn.execute(
        "SELECT match_id, puuid FROM participants WHERE champion_name='Gwen'").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 1, "timestamp_ms": 100, "detail": ""},
        {"event_type": "kill", "x": 2, "y": 2, "timestamp_ms": 200, "detail": ""},
        {"event_type": "tower", "x": 3, "y": 3, "timestamp_ms": 300, "detail": "Outer tower"},
    ])
    events = stats.map_events(conn, [row["puuid"]])
    assert [e["event_type"] for e in events] == ["death"]


# ---------- Ascent log events (the offline source) ----------

LOG_SNAPSHOT = """
[2026-07-30T10:23:47] [INFO] [GEP] event=MatchStarted game_id=1 match_id="EUW1_1"
[2026-07-30T10:24:00] [INFO] snapshot {"allPlayers":[
 {"summonerName":"Me#EUW","team":"ORDER"},
 {"summonerName":"Ally#EUW","team":"ORDER"},
 {"summonerName":"Enemy#EUW","team":"CHAOS"}],
 "events":[
 {"EventID":0,"EventName":"GameStart","EventTime":0.0},
 {"EventID":1,"EventName":"ChampionKill","EventTime":60.0,
  "KillerName":{"Summoner":"Me#EUW"},"VictimName":"Enemy#EUW","Assisters":[]},
 {"EventID":2,"EventName":"ChampionKill","EventTime":120.0,
  "KillerName":{"Summoner":"Enemy#EUW"},"VictimName":"Me#EUW","Assisters":[]},
 {"EventID":3,"EventName":"ChampionKill","EventTime":180.0,
  "KillerName":{"Summoner":"Ally#EUW"},"VictimName":"Enemy#EUW","Assisters":["Me#EUW"]},
 {"EventID":4,"EventName":"ChampionKill","EventTime":200.0,
  "KillerName":{"Summoner":"Enemy#EUW"},"VictimName":"Ally#EUW","Assisters":[]},
 {"EventID":5,"EventName":"TurretKilled","EventTime":300.0,
  "KillerName":{"Summoner":"Ally#EUW"},"TurretKilled":"Unknown"},
 {"EventID":6,"EventName":"DragonKill","EventTime":400.0,"DragonType":"Infernal",
  "KillerName":{"Summoner":"Enemy#EUW"}},
 {"EventID":7,"EventName":"FirstBlood","EventTime":60.0,"Recipient":"Me#EUW"}]}
[2026-07-30T10:50:00] [INFO] [GEP] event=MatchEnded game_id=1 match_id="EUW1_1"
"""


def write_log(tmp_path, match_id="EUW1_1", text=LOG_SNAPSHOT):
    """`add_match`'s id counter is module-global, so a test that pairs a log
    with a real match must stamp the log with whatever id it actually got."""
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    (d / "app.log").write_text(text.replace("EUW1_1", match_id), encoding="utf-8")
    return d


def test_log_events_are_attributed_to_the_right_match(tmp_path):
    from server import ascent_log
    found = ascent_log.read_log_events(write_log(tmp_path))
    assert list(found) == ["EUW1_1"]
    assert len(found["EUW1_1"]["events"]) == 8
    assert len(found["EUW1_1"]["players"]) == 3


def test_log_events_classify_kills_deaths_and_assists(tmp_path):
    from server import ascent_log
    raw = ascent_log.read_log_events(write_log(tmp_path))["EUW1_1"]
    events = ascent_log.to_map_events(raw["events"], ["Me#EUW"], raw["players"])
    kinds = [(e["event_type"], e["detail"]) for e in events]
    assert ("kill", "") in kinds
    assert ("death", "") in kinds
    assert ("assist", "") in kinds
    # a kill between two other players, that we had no part in, is not a chapter
    assert len([k for k in kinds if k[0] in ("kill", "death", "assist")]) == 3


def test_log_events_use_the_team_not_just_the_killer(tmp_path):
    """A tower an ALLY took is ours; a dragon the enemy took is not."""
    from server import ascent_log
    raw = ascent_log.read_log_events(write_log(tmp_path))["EUW1_1"]
    events = ascent_log.to_map_events(raw["events"], ["Me#EUW"], raw["players"])
    details = {e["detail"] for e in events}
    assert "Tower" in details            # ally's tower, still ours
    assert "-Infernal dragon" in details  # enemy's dragon


def test_log_events_carry_no_position(tmp_path):
    """Live Client Data has no coordinates — these must never reach the map."""
    from server import ascent_log
    raw = ascent_log.read_log_events(write_log(tmp_path))["EUW1_1"]
    events = ascent_log.to_map_events(raw["events"], ["Me#EUW"], raw["players"])
    assert all(e["x"] is None and e["y"] is None for e in events)


def test_log_derived_events_are_excluded_from_the_heatmap(tmp_path, conn):
    from server import stats
    add_match(conn, my_champ="Gwen", when=1_700_000_000_000)
    row = conn.execute(
        "SELECT match_id, puuid FROM participants WHERE champion_name='Gwen'").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": None, "y": None,
         "timestamp_ms": 100, "detail": ""}], source="ascent_log")
    assert stats.map_events(conn, [row["puuid"]]) == []


def test_timeline_import_supersedes_log_events(conn):
    """Both sources describe the same game; the timeline wins because it also
    knows where things happened."""
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "kill", "x": None, "y": None, "timestamp_ms": 100, "detail": ""},
        {"event_type": "tower", "x": None, "y": None, "timestamp_ms": 200, "detail": "Tower"},
    ], source="ascent_log")
    assert db.has_log_events(conn, row["match_id"], row["puuid"]) is True

    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 5, "y": 6, "timestamp_ms": 300, "detail": ""},
    ], source="timeline")
    rows = conn.execute(
        "SELECT event_type, source FROM player_map_events WHERE match_id=? AND puuid=?",
        (row["match_id"], row["puuid"])).fetchall()
    assert [(r["event_type"], r["source"]) for r in rows] == [("death", "timeline")]
    assert db.has_log_events(conn, row["match_id"], row["puuid"]) is False


def test_log_import_does_not_disturb_timeline_events(conn):
    """...and the reverse must not happen: a log sync must not wipe the richer
    timeline rows it can't replace."""
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 5, "y": 6, "timestamp_ms": 300, "detail": ""},
    ], source="timeline")
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "kill", "x": None, "y": None, "timestamp_ms": 100, "detail": ""},
    ], source="ascent_log")
    sources = {r["source"] for r in conn.execute(
        "SELECT source FROM player_map_events WHERE match_id=?", (row["match_id"],))}
    assert sources == {"timeline", "ascent_log"}


def test_log_sync_stores_events_for_tracked_players(tmp_path, conn):
    from server import ascent_log
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    # events are only stored for tracked accounts, and the log identifies
    # players by Riot ID — so the player row has to exist and match
    db.upsert_player(conn, row["puuid"], "Me", "EUW", is_tracked=True)
    result = ascent_log.sync(conn, write_log(tmp_path, row["match_id"]), ["Me#EUW"])
    assert result["matches_in_logs"] == 1
    assert result["imported"] == 1
    stored = conn.execute(
        "SELECT COUNT(*) c FROM player_map_events WHERE source='ascent_log'").fetchone()["c"]
    assert stored == result["events"] > 0


def test_missing_log_directory_is_reported(tmp_path):
    from server import ascent_log
    with pytest.raises(FileNotFoundError):
        ascent_log.read_log_events(tmp_path / "nope")


def test_timestamp_formatting_matches_what_youtube_parses():
    assert recordings.fmt_timestamp(0) == "0:00"
    assert recordings.fmt_timestamp(65_000) == "1:05"
    assert recordings.fmt_timestamp(742_000) == "12:22"
    assert recordings.fmt_timestamp(3_725_000) == "1:02:05"  # H:MM:SS past an hour
    assert recordings.fmt_timestamp(-5) == "0:00"


def test_chapters_always_open_at_zero():
    """YouTube ignores the whole chapter list unless the first one is 0:00."""
    chapters = recordings.build_chapters([{"video_ms": 300_000}])
    assert chapters[0] == (0, "Game start")


def test_chapters_drop_marks_closer_than_youtubes_minimum():
    marks = [{"video_ms": 5_000},      # < 10s after 0:00
             {"video_ms": 300_000},
             {"video_ms": 305_000},    # < 10s after the previous
             {"video_ms": 400_000}]
    chapters = recordings.build_chapters(marks)
    assert [c[0] for c in chapters] == [0, 300_000, 400_000]
    # Numbering counts deaths in the GAME, not chapters in the list, so it
    # lines up with the KDA in the description header — a merged-away death
    # still consumes its number.
    assert [c[1] for c in chapters] == ["Game start", "Death 2", "Death 4"]


def test_chapters_drop_marks_past_the_end_of_the_video():
    chapters = recordings.build_chapters(
        [{"video_ms": 60_000}, {"video_ms": 9_999_000}], duration_s=120)
    assert [c[0] for c in chapters] == [0, 60_000]


def test_description_has_the_matchup_and_a_chapter_per_death(tmp_path, conn):
    add_match(conn, my_champ="Gwen", opp_champ="Darius", win=True,
              when=1_700_000_000_000, kills=7, deaths=2, assists=5)
    row = conn.execute(
        "SELECT match_id, puuid FROM participants WHERE champion_name='Gwen'").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 1, "timestamp_ms": 305_000},
        {"event_type": "death", "x": 2, "y": 2, "timestamp_ms": 742_000},
    ])
    db.upsert_recording(conn, {
        "uuid": "r1", "match_id": row["match_id"], "video_path": "x.mp4",
        "started_at_ms": 1_700_000_030_000, "duration_s": 1800.0})
    text = recordings.build_description(
        conn, db.get_recording(conn, "r1"), row["puuid"])

    assert "Gwen vs Darius" in text
    assert "Win" in text and "7/2/5" in text
    assert text.count("\n0:00 Game start") == 1 or text.startswith("0:00 Game start")
    assert "5:05 Death 1" in text
    assert "12:22 Death 2" in text


def test_description_offset_shifts_the_chapters(tmp_path, conn):
    add_match(conn, when=1_700_000_000_000)
    row = conn.execute("SELECT match_id, puuid FROM participants LIMIT 1").fetchone()
    db.replace_map_events(conn, row["match_id"], row["puuid"], [
        {"event_type": "death", "x": 1, "y": 1, "timestamp_ms": 305_000}])
    db.upsert_recording(conn, {
        "uuid": "r1", "match_id": row["match_id"], "video_path": "x.mp4",
        "started_at_ms": 1_700_000_030_000, "duration_s": 1800.0})
    db.set_recording_offset(conn, "r1", 30_000)  # video runs 30s behind the game
    text = recordings.build_description(
        conn, db.get_recording(conn, "r1"), row["puuid"])
    assert "5:35 Death 1" in text


def test_reveal_opens_the_file_manager_without_a_shell(tmp_path):
    """The no-setup upload path. Must never go through a shell — the path comes
    from a database row."""
    video = tmp_path / "game.mp4"
    video.write_bytes(b"x")
    with mock.patch("subprocess.Popen") as popen:
        assert recordings.reveal_in_file_manager(video) is True
    args, kwargs = popen.call_args
    assert isinstance(args[0], list)          # argument vector, not a string
    assert kwargs.get("shell") in (None, False)
    assert str(video) in " ".join(args[0])


def test_reveal_reports_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        recordings.reveal_in_file_manager(tmp_path / "gone.mp4")


# ---------- YouTube ----------

def test_upload_validates_before_touching_the_network():
    with pytest.raises(youtube.YouTubeError, match="privacy must be one of"):
        youtube.upload("x.mp4", "t", privacy="everyone")


def test_upload_requires_configuration(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(youtube.YouTubeError, match="No YouTube client secrets"):
        youtube.upload(video, "t", client_secrets_path=None, db_dir=tmp_path)


def test_upload_builds_the_request_and_clamps_fields(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 128)
    captured = {}

    class Request:
        def next_chunk(self):
            return None, {"id": "VID"}

    class Videos:
        def insert(self, part, body, media_body):
            captured.update(part=part, body=body, media=media_body)
            return Request()

    with mock.patch.object(youtube, "_credentials", return_value="C"), \
         mock.patch("googleapiclient.discovery.build",
                    return_value=mock.Mock(videos=lambda: Videos())):
        video_id = youtube.upload(video, "T" * 200, "D" * 6000, "unlisted",
                                  client_secrets_path="x", db_dir=tmp_path)

    assert video_id == "VID"
    assert len(captured["body"]["snippet"]["title"]) == youtube.MAX_TITLE
    assert len(captured["body"]["snippet"]["description"]) == youtube.MAX_DESCRIPTION
    assert captured["body"]["status"]["privacyStatus"] == "unlisted"
    assert captured["media"].resumable() is True  # these files are gigabytes


def test_default_privacy_is_private():
    """Unaudited OAuth projects can only produce private videos anyway."""
    assert youtube.DEFAULT_PRIVACY == "private"
