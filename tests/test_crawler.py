import json

import pytest

from server import db
from server.crawler import Crawler

TRACKED_PUUID = "tracked-1"


def match_json(match_id, creation_ms, queue_id=420, tracked_pos="TOP",
               opp_puuid="opp-1", opp_pos="TOP", duration_s=1800):
    """Minimal-but-valid match-v5 JSON with 10 participants."""
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    participants = []
    for i, pos in enumerate(positions):
        puuid = TRACKED_PUUID if pos == tracked_pos else f"ally-{i}"
        participants.append({
            "puuid": puuid, "riotIdGameName": f"A{i}", "championName": "Garen",
            "teamId": 100, "teamPosition": pos, "win": True,
            "kills": 1, "deaths": 1, "assists": 1,
            "totalMinionsKilled": 100, "neutralMinionsKilled": 0,
            "goldEarned": 10000, "totalDamageDealtToChampions": 10000,
        })
    for i, pos in enumerate(positions):
        puuid = opp_puuid if pos == opp_pos else f"enemy-{i}"
        participants.append({
            "puuid": puuid, "riotIdGameName": f"E{i}", "championName": "Darius",
            "teamId": 200, "teamPosition": pos, "win": False,
            "kills": 1, "deaths": 1, "assists": 1,
            "totalMinionsKilled": 100, "neutralMinionsKilled": 0,
            "goldEarned": 10000, "totalDamageDealtToChampions": 10000,
        })
    return {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in participants]},
        "info": {
            "gameCreation": creation_ms, "gameDuration": duration_s,
            "gameVersion": "14.1.1", "queueId": queue_id,
            "participants": participants,
        },
    }


def timeline_json(match_id, me_puuid=TRACKED_PUUID, opp_puuid="opp-1"):
    """Minimal match-v5 timeline: participantIds 1 (me) and 2 (opp), frames
    at 0/7/14 min where `me` leads the opponent."""
    def frame(ts, mine, theirs):
        return {"timestamp": ts, "participantFrames": {
            "1": dict(zip(("minionsKilled", "jungleMinionsKilled", "level", "totalGold"), mine)),
            "2": dict(zip(("minionsKilled", "jungleMinionsKilled", "level", "totalGold"), theirs))}}
    return {"metadata": {"matchId": match_id}, "info": {
        "participants": [{"participantId": 1, "puuid": me_puuid},
                         {"participantId": 2, "puuid": opp_puuid}],
        "frames": [
            frame(0, (0, 0, 1, 500), (0, 0, 1, 500)),
            frame(420_000, (50, 5, 6, 2600), (40, 0, 5, 2200)),
            frame(840_000, (110, 10, 10, 5300), (90, 0, 9, 4500)),
        ]}}


class FakeClient:
    def __init__(self, matches, timelines=None):
        # matches: list of match JSON, any order; served newest-first like Riot
        self.matches = {m["metadata"]["matchId"]: m for m in matches}
        self.timelines = {t["metadata"]["matchId"]: t for t in (timelines or [])}
        self.detail_calls = 0
        self.timeline_calls = 0
        self.league_calls = []
        self.ranks = {}  # puuid -> list of league entries

    def get_match_timeline(self, match_id):
        self.timeline_calls += 1
        if match_id not in self.timelines:
            raise KeyError(match_id)  # exercises the crawler's tolerant fetch
        return self.timelines[match_id]

    def get_account(self, game_name, tag_line):
        return {"puuid": TRACKED_PUUID, "gameName": game_name, "tagLine": tag_line}

    def get_match_ids(self, puuid, queue=None, start=0, count=100, start_time=None, end_time=None):
        ms = [
            m for m in self.matches.values()
            if m["info"]["queueId"] == queue
            and (start_time is None or m["info"]["gameCreation"] >= start_time * 1000)
        ]
        ms.sort(key=lambda m: -m["info"]["gameCreation"])
        ids = [m["metadata"]["matchId"] for m in ms]
        return ids[start:start + count]

    def get_match(self, match_id):
        self.detail_calls += 1
        return self.matches[match_id]

    def get_league_entries(self, puuid):
        self.league_calls.append(puuid)
        return self.ranks.get(puuid, [])


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def make_crawler(client, conn, now_ms=1_800_000_000_000):
    return Crawler(client, conn, now_ms=lambda: now_ms)


def test_crawl_inserts_matches_and_participants(conn):
    client = FakeClient([
        match_json("EUW1_1", 1_700_000_000_000),
        match_json("EUW1_2", 1_700_000_100_000),
    ])
    result = make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    assert result["new_matches"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM participants").fetchone()["c"] == 20
    player = conn.execute("SELECT * FROM players WHERE puuid=?", (TRACKED_PUUID,)).fetchone()
    assert player["is_tracked"] == 1
    assert player["game_name"] == "PlayerOne"


def test_second_crawl_is_incremental_and_fetches_no_details(conn):
    client = FakeClient([
        match_json("EUW1_1", 1_700_000_000_000),
        match_json("EUW1_2", 1_700_000_100_000),
    ])
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    client.detail_calls = 0
    result = crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    assert client.detail_calls == 0
    assert result["new_matches"] == 0
    # a match played after the first crawl gets picked up
    client.matches["EUW1_3"] = match_json("EUW1_3", 1_700_000_200_000)
    result = crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    assert result["new_matches"] == 1


def test_limit_caps_new_detail_fetches_and_leaves_incomplete(conn):
    client = FakeClient([
        match_json(f"EUW1_{i}", 1_700_000_000_000 + i * 1000) for i in range(5)
    ])
    crawler = make_crawler(client, conn)
    result = crawler.crawl_player("PlayerOne", "EUW", queues=(420,), limit=2)
    assert result["new_matches"] == 2
    _, complete = db.get_crawl_watermark(conn, TRACKED_PUUID, 420)
    assert complete is False
    # next run without limit picks up the remaining 3
    result = crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    assert result["new_matches"] == 3
    _, complete = db.get_crawl_watermark(conn, TRACKED_PUUID, 420)
    assert complete is True


def test_enrich_ranks_fetches_top_lane_opponents(conn):
    client = FakeClient([
        match_json("EUW1_1", 1_700_000_000_000, opp_puuid="opp-A"),
        match_json("EUW1_2", 1_700_000_100_000, opp_puuid="opp-B"),
    ])
    client.ranks["opp-A"] = [
        {"queueType": "RANKED_FLEX_SR", "tier": "SILVER", "rank": "I", "leaguePoints": 10},
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "III", "leaguePoints": 42},
    ]
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    n = crawler.enrich_ranks()
    assert n == 2
    assert sorted(client.league_calls) == ["opp-A", "opp-B"]
    row = db.get_player_rank(conn, "opp-A")
    assert (row["solo_tier"], row["solo_division"], row["solo_lp"]) == ("GOLD", "III", 42)
    # opp-B had no solo entry -> stored as unranked
    assert db.get_player_rank(conn, "opp-B")["solo_tier"] is None


def test_enrich_ranks_skips_fresh_entries(conn):
    client = FakeClient([match_json("EUW1_1", 1_700_000_000_000, opp_puuid="opp-A")])
    now = 1_800_000_000_000
    crawler = make_crawler(client, conn, now_ms=now)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    db.set_player_rank(conn, "opp-A", "GOLD", "I", 1, fetched_at_ms=now - 1000)  # fresh
    assert crawler.enrich_ranks() == 0
    db.set_player_rank(conn, "opp-A", "GOLD", "I", 1, fetched_at_ms=now - 8 * 86_400_000)  # stale
    assert crawler.enrich_ranks() == 1


def test_crawl_stores_metrics_for_tracked_participant(conn):
    match = match_json("EUW1_1", 1_700_000_000_000)
    tracked = next(p for p in match["info"]["participants"]
                   if p["puuid"] == TRACKED_PUUID)
    tracked["challenges"] = {"laneMinionsFirst10Minutes": 81, "soloKills": 1}
    tracked["totalTimeSpentDead"] = 120
    client = FakeClient([match])
    make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    row = conn.execute(
        "SELECT * FROM participant_metrics WHERE match_id='EUW1_1' AND puuid=?",
        (TRACKED_PUUID,)).fetchone()
    assert row["cs_at_10"] == 81
    assert row["time_dead"] == 120
    # opponents don't get metric rows
    assert conn.execute("SELECT COUNT(*) c FROM participant_metrics").fetchone()["c"] == 1


PERKS = {
    "statPerks": {"offense": 5008, "flex": 5002, "defense": 5011},
    "styles": [
        {"description": "primaryStyle", "style": 8000, "selections": [
            {"perk": 8010}, {"perk": 9111}, {"perk": 9104}, {"perk": 8299}]},
        {"description": "subStyle", "style": 8400, "selections": [
            {"perk": 8473}, {"perk": 8451}]},
    ],
}


def test_crawl_stores_runes_for_tracked_participant_and_lane_opponent(conn):
    match = match_json("EUW1_1", 1_700_000_000_000)  # opp_pos defaults to TOP, shares the lane
    tracked = next(p for p in match["info"]["participants"]
                   if p["puuid"] == TRACKED_PUUID)
    tracked["perks"] = PERKS
    client = FakeClient([match])
    make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    row = conn.execute(
        "SELECT runes FROM participant_runes WHERE match_id='EUW1_1' AND puuid=?",
        (TRACKED_PUUID,)).fetchone()
    runes = json.loads(row["runes"])
    assert runes["primary_tree"] == "Precision"
    assert runes["keystone"] == "Conqueror"
    assert runes["secondary_tree"] == "Resolve"
    # the lane opponent (same teamPosition, other team) also gets a row —
    # blank here since this fixture doesn't set perks for them
    opp_row = conn.execute(
        "SELECT runes FROM participant_runes WHERE match_id='EUW1_1' AND puuid='opp-1'").fetchone()
    assert opp_row["runes"] == ""
    # only the tracked participant + their lane opponent — not all 10 players
    assert conn.execute("SELECT COUNT(*) c FROM participant_runes").fetchone()["c"] == 2


def test_crawl_stores_blank_runes_row_when_no_perks_data(conn):
    match = match_json("EUW1_1", 1_700_000_000_000)  # no perks field at all
    client = FakeClient([match])
    make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    row = conn.execute(
        "SELECT runes FROM participant_runes WHERE match_id='EUW1_1' AND puuid=?",
        (TRACKED_PUUID,)).fetchone()
    assert row["runes"] == ""  # row exists (blank) so backfill won't keep re-fetching it


def test_backfill_runes_fetches_missing_only(conn):
    m1 = match_json("EUW1_1", 1_700_000_000_000)
    m2 = match_json("EUW1_2", 1_700_000_100_000)
    for m in (m1, m2):
        p = next(q for q in m["info"]["participants"] if q["puuid"] == TRACKED_PUUID)
        p["perks"] = PERKS
    client = FakeClient([m1, m2])
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))  # runes stored inline
    client.detail_calls = 0
    assert crawler.backfill_runes() == 0             # nothing missing
    assert client.detail_calls == 0
    conn.execute("DELETE FROM participant_runes WHERE match_id='EUW1_1'")
    conn.commit()
    assert crawler.backfill_runes() == 1             # only the missing match refetched
    assert client.detail_calls == 1
    # 2 rows per match (tracked + lane opponent) x 2 matches
    assert conn.execute("SELECT COUNT(*) c FROM participant_runes").fetchone()["c"] == 4


def test_crawl_stores_lane_deltas_inline_from_timeline(conn):
    match = match_json("EUW1_1", 1_700_000_000_000)  # opp_pos TOP shares the lane
    client = FakeClient([match], timelines=[timeline_json("EUW1_1")])
    make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    row = conn.execute(
        """SELECT has_timeline, cs_diff_7, level_diff_7, gold_diff_7,
                  cs_diff_14, gold_diff_14 FROM participant_metrics
           WHERE match_id='EUW1_1' AND puuid=?""", (TRACKED_PUUID,)).fetchone()
    assert row["has_timeline"] == 1
    assert row["cs_diff_7"] == (50 + 5) - 40   # 15
    assert row["level_diff_7"] == 1
    assert row["gold_diff_7"] == 400
    assert row["cs_diff_14"] == (110 + 10) - 90  # 30
    assert row["gold_diff_14"] == 800


def test_crawl_tolerates_missing_timeline(conn):
    match = match_json("EUW1_1", 1_700_000_000_000)
    client = FakeClient([match])  # no timelines — get_match_timeline raises
    make_crawler(client, conn).crawl_player("PlayerOne", "EUW", queues=(420,))
    row = conn.execute(
        "SELECT has_timeline, cs_diff_7 FROM participant_metrics WHERE puuid=?",
        (TRACKED_PUUID,)).fetchone()
    assert row["has_timeline"] == 1  # marked done so backfill won't retry forever
    assert row["cs_diff_7"] is None  # no timeline data available


def test_backfill_lane_deltas_fills_missing_only(conn):
    m1 = match_json("EUW1_1", 1_700_000_000_000)
    m2 = match_json("EUW1_2", 1_700_000_100_000)
    # crawl with no timelines available, so metrics rows exist but has_timeline=0
    client = FakeClient([m1, m2])
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    assert conn.execute(
        "SELECT COUNT(*) c FROM participant_metrics WHERE has_timeline=1").fetchone()["c"] == 2
    # ^ tolerant fetch already marked them done; reset to simulate pre-upgrade rows
    conn.execute("UPDATE participant_metrics SET has_timeline=0, cs_diff_7=NULL")
    conn.commit()
    # now timelines are available for the backfill
    client.timelines = {t["metadata"]["matchId"]: t
                        for t in (timeline_json("EUW1_1"), timeline_json("EUW1_2"))}
    client.timeline_calls = 0
    assert crawler.backfill_lane_deltas() == 2
    assert client.timeline_calls == 2
    assert crawler.backfill_lane_deltas() == 0  # nothing left with has_timeline=0
    row = conn.execute(
        "SELECT cs_diff_7 FROM participant_metrics WHERE match_id='EUW1_1' AND puuid=?",
        (TRACKED_PUUID,)).fetchone()
    assert row["cs_diff_7"] == 15
    # backfill must not have wiped the challenge-derived metrics on the row
    other = conn.execute(
        "SELECT COUNT(*) c FROM participant_metrics WHERE has_timeline=1").fetchone()["c"]
    assert other == 2


def test_backfill_lane_deltas_block_games_only(conn):
    m1 = match_json("EUW1_1", 1_700_000_000_000)  # will be added to a block
    m2 = match_json("EUW1_2", 1_700_000_100_000)  # not in any block
    client = FakeClient([m1, m2])  # no timelines during crawl -> has_timeline=0 after reset
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    conn.execute("UPDATE participant_metrics SET has_timeline=0")
    conn.commit()
    db.add_game_to_block(conn, "EUW1_1", TRACKED_PUUID)  # only m1 is in a block
    client.timelines = {t["metadata"]["matchId"]: t
                        for t in (timeline_json("EUW1_1"), timeline_json("EUW1_2"))}
    client.timeline_calls = 0
    # block-only backfill touches just the block game
    assert crawler.backfill_lane_deltas(block_games_only=True) == 1
    assert client.timeline_calls == 1
    assert conn.execute("SELECT cs_diff_7 FROM participant_metrics WHERE match_id='EUW1_1' "
                        "AND puuid=?", (TRACKED_PUUID,)).fetchone()["cs_diff_7"] == 15
    assert conn.execute("SELECT has_timeline FROM participant_metrics WHERE match_id='EUW1_2' "
                        "AND puuid=?", (TRACKED_PUUID,)).fetchone()["has_timeline"] == 0
    # an unscoped run then handles the remaining (non-block) game
    assert crawler.backfill_lane_deltas() == 1


def test_backfill_metrics_fetches_missing_only(conn):
    m1 = match_json("EUW1_1", 1_700_000_000_000)
    m2 = match_json("EUW1_2", 1_700_000_100_000)
    for m in (m1, m2):
        p = next(q for q in m["info"]["participants"] if q["puuid"] == TRACKED_PUUID)
        p["challenges"] = {"laneMinionsFirst10Minutes": 70}
    client = FakeClient([m1, m2])
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))  # metrics stored inline
    client.detail_calls = 0
    assert crawler.backfill_metrics() == 0          # nothing missing
    assert client.detail_calls == 0
    conn.execute("DELETE FROM participant_metrics WHERE match_id='EUW1_1'")
    conn.commit()
    assert crawler.backfill_metrics() == 1          # only the missing one refetched
    assert client.detail_calls == 1
    assert conn.execute("SELECT COUNT(*) c FROM participant_metrics").fetchone()["c"] == 2


def test_backfill_metrics_respects_limit(conn):
    matches = [match_json(f"EUW1_{i}", 1_700_000_000_000 + i) for i in range(3)]
    client = FakeClient(matches)
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    conn.execute("DELETE FROM participant_metrics")
    conn.commit()
    assert crawler.backfill_metrics(limit=2) == 2


def test_refresh_tracked_ranks_updates_players_table(conn):
    client = FakeClient([match_json("EUW1_1", 1_700_000_000_000)])
    client.ranks[TRACKED_PUUID] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "DIAMOND", "rank": "IV", "leaguePoints": 12},
    ]
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    crawler.refresh_tracked_ranks()
    row = conn.execute("SELECT * FROM players WHERE puuid=?", (TRACKED_PUUID,)).fetchone()
    assert (row["solo_tier"], row["solo_division"], row["solo_lp"]) == ("DIAMOND", "IV", 12)


def test_refresh_tracked_ranks_appends_rank_history(conn):
    client = FakeClient([match_json("EUW1_1", 1_700_000_000_000)])
    client.ranks[TRACKED_PUUID] = [
        {"queueType": "RANKED_SOLO_5x5", "tier": "DIAMOND", "rank": "IV", "leaguePoints": 12},
    ]
    crawler = make_crawler(client, conn)
    crawler.crawl_player("PlayerOne", "EUW", queues=(420,))
    crawler.refresh_tracked_ranks()
    client.ranks[TRACKED_PUUID][0]["leaguePoints"] = 30
    crawler.now_ms = lambda: 1_800_000_100_000
    crawler.refresh_tracked_ranks()
    rows = conn.execute(
        "SELECT * FROM rank_history WHERE puuid=? ORDER BY fetched_at_ms",
        (TRACKED_PUUID,)).fetchall()
    assert [(r["solo_tier"], r["solo_lp"], r["fetched_at_ms"]) for r in rows] == [
        ("DIAMOND", 12, 1_800_000_000_000), ("DIAMOND", 30, 1_800_000_100_000)]


def test_comparison_player_stored_without_tracking(conn):
    """A comparison ('research') player is crawled with is_tracked=False: their
    match metrics + runes are stored (so the guide can compare against them),
    but their players row stays untracked, keeping them out of tracked stats."""
    match = match_json("EUW1_1", 1_700_000_000_000)  # opp shares TOP lane
    tracked = next(p for p in match["info"]["participants"]
                   if p["puuid"] == TRACKED_PUUID)
    tracked["challenges"] = {"laneMinionsFirst10Minutes": 77}
    tracked["perks"] = PERKS
    db.add_comparison_player(conn, TRACKED_PUUID, "Rival", "EUW")
    make_crawler(FakeClient([match]), conn).crawl_player(
        "Rival", "EUW", queues=(420,), is_tracked=False)
    assert conn.execute("SELECT is_tracked FROM players WHERE puuid=?",
                        (TRACKED_PUUID,)).fetchone()["is_tracked"] == 0
    assert conn.execute("SELECT cs_at_10 FROM participant_metrics WHERE puuid=?",
                        (TRACKED_PUUID,)).fetchone()["cs_at_10"] == 77
    runes = conn.execute("SELECT runes FROM participant_runes WHERE puuid=?",
                         (TRACKED_PUUID,)).fetchone()
    assert json.loads(runes["runes"])["primary_tree"] == "Precision"
