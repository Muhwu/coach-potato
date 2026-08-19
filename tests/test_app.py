import pytest
from fastapi.testclient import TestClient

from server import db
from server import app as app_module
from server.app import app, parse_time_range

from tests.test_stats import ME, add_match  # reuse fixture builder


@pytest.fixture
def client(tmp_path, monkeypatch):
    from server import config
    db_path = tmp_path / "t.sqlite"
    monkeypatch.setenv("LOL_DB_PATH", str(db_path))
    monkeypatch.setattr(config, "ENV_FALLBACK_ROOT", tmp_path)  # ignore repo .env
    conn = db.connect(db_path)
    db.upsert_player(conn, ME, "PlayerOne", "EUW", is_tracked=True)
    add_match(conn, my_champ="Garen", opp_champ="Darius", win=True, when=1_700_000_000_000)
    add_match(conn, my_champ="Garen", opp_champ="Darius", win=False, when=1_700_000_100_000)
    add_match(conn, my_champ="Kled", opp_champ="Teemo", win=True, when=1_600_000_000_000, queue=440)
    conn.close()
    with TestClient(app) as c:
        yield c


def test_parse_time_range_presets():
    now_ms = 1_700_000_000_000
    from_ms, to_ms = parse_time_range({"range": "7d"}, now_ms=now_ms)
    assert from_ms == now_ms - 7 * 86_400_000
    assert to_ms is None
    assert parse_time_range({"range": "all"}, now_ms=now_ms) == (None, None)
    assert parse_time_range({}, now_ms=now_ms) == (None, None)


def test_parse_time_range_explicit_dates():
    from_ms, to_ms = parse_time_range({"from": "2024-01-01", "to": "2024-02-01"})
    assert from_ms == 1_704_067_200_000
    # 'to' is inclusive: end of that day
    assert to_ms == 1_706_745_600_000 + 86_400_000 - 1


def test_players_endpoint(client):
    players = client.get("/api/players").json()
    assert len(players) == 1
    assert players[0]["game_name"] == "PlayerOne"
    assert players[0]["puuid"] == ME
    assert players[0]["total_matches"] == 3


def test_matchups_endpoint_with_filters(client):
    rows = client.get(f"/api/stats/matchups?puuid={ME}").json()
    assert {r["opp_champion"] for r in rows} == {"Darius", "Teemo"}
    rows = client.get(f"/api/stats/matchups?puuid={ME}&champion=Kled").json()
    assert [r["opp_champion"] for r in rows] == ["Teemo"]
    rows = client.get(f"/api/stats/matchups?puuid={ME}&queue=440").json()
    assert [r["opp_champion"] for r in rows] == ["Teemo"]
    rows = client.get(f"/api/stats/matchups?puuid={ME}&from=2023-11-01").json()
    assert [r["opp_champion"] for r in rows] == ["Darius"]


def test_summary_endpoint(client):
    s = client.get(f"/api/stats/summary?puuid={ME}").json()
    assert s["games"] == 3
    assert len(s["by_champion"]) == 2


def test_matchups_by_rank_endpoint(client):
    rows = client.get(f"/api/stats/matchups_by_rank?puuid={ME}").json()
    assert all(r["rank_tier"] == "UNKNOWN" for r in rows)


def test_filters_endpoint(client):
    opts = client.get(f"/api/filters?puuid={ME}").json()
    assert set(opts["champions"]) == {"Garen", "Kled"}


def test_crawl_status_shape(client):
    status = client.get("/api/crawl/status").json()
    assert status["running"] is False
    assert "message" in status
    assert status["rate_limited"] is False


def test_crawl_conflict_when_already_running(client):
    app_module.CRAWL_STATE["running"] = True
    try:
        response = client.post("/api/crawl")
        assert response.status_code == 409
    finally:
        app_module.CRAWL_STATE["running"] = False


def test_sessions_crud_round_trip(client):
    assert client.get("/api/sessions").json() == []
    response = client.post("/api/sessions",
                           json={"date": "2026-06-28", "title": "waves", "notes": "# Md"})
    assert response.status_code == 200
    session_id = response.json()["id"]
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["session_date"] == "2026-06-28"
    assert sessions[0]["title"] == "waves"
    assert sessions[0]["notes"] == "# Md"
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get("/api/sessions").json() == []


def test_patch_session_updates_title_and_notes(client):
    session_id = client.post("/api/sessions", json={"date": "2026-06-28", "title": "old"}).json()["id"]
    response = client.patch(f"/api/sessions/{session_id}", json={"title": "new", "notes": "**bold**"})
    assert response.status_code == 200
    row = client.get("/api/sessions").json()[0]
    assert row["title"] == "new"
    assert row["notes"] == "**bold**"
    # partial patch keeps other field
    client.patch(f"/api/sessions/{session_id}", json={"notes": "only notes"})
    row = client.get("/api/sessions").json()[0]
    assert row["title"] == "new"
    assert row["notes"] == "only notes"


def test_patch_session_errors(client):
    assert client.patch("/api/sessions/999", json={"title": "x"}).status_code == 404
    session_id = client.post("/api/sessions", json={"date": "2026-06-28"}).json()["id"]
    assert client.patch(f"/api/sessions/{session_id}", json={}).status_code == 400


def test_export_markdown_document(client):
    client.post("/api/sessions", json={"date": "2026-06-28", "title": "a", "notes": "- worked on waves"})
    client.post("/api/sessions", json={"date": "2026-07-05", "title": "b", "notes": "- trading stance"})
    response = client.get("/api/sessions/export.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "coaching-sessions.md" in response.headers["content-disposition"]
    body = response.text
    assert body.startswith("# Coaching sessions")
    assert body.index("## 2026-07-05 — b") < body.index("## 2026-06-28 — a")
    assert "- trading stance" in body


def test_export_untitled_session_and_empty_db(client):
    response = client.get("/api/sessions/export.md")
    assert response.status_code == 200
    assert response.text.strip() == "# Coaching sessions"
    client.post("/api/sessions", json={"date": "2026-06-28"})
    assert "## 2026-06-28 — Session" in client.get("/api/sessions/export.md").text


def test_post_session_invalid_date_400(client):
    assert client.post("/api/sessions", json={"date": "28.6.2026"}).status_code == 400
    assert client.post("/api/sessions", json={}).status_code == 400


def test_post_session_duplicate_409(client):
    client.post("/api/sessions", json={"date": "2026-06-28"})
    assert client.post("/api/sessions", json={"date": "2026-06-28"}).status_code == 409


def test_delete_missing_session_404(client):
    assert client.delete("/api/sessions/999").status_code == 404


def test_progress_endpoint(client):
    client.post("/api/sessions", json={"date": "2023-11-01", "note": "n1"})
    segments = client.get("/api/stats/progress").json()
    assert [s["label"] for s in segments] == ["Baseline", "Since 2023-11-01"]
    # the two Garen games (2023-11-14) fall after the session; Kled (2020) in neither
    assert segments[1]["games"] == 2
    filtered = client.get("/api/stats/progress?champion=Garen").json()
    assert filtered[1]["games"] == 2
    filtered = client.get("/api/stats/progress?champion=Kled").json()
    assert filtered[1]["games"] == 0


def test_games_endpoint_lists_games_with_account(client):
    games = client.get("/api/stats/games").json()
    assert len(games) == 3
    assert all(g["account"] == "PlayerOne" for g in games)
    assert games[0]["game_creation_ms"] >= games[-1]["game_creation_ms"]
    assert {"my_champion", "opp_champion", "rank_tier", "win", "kills",
            "cs", "game_duration_s"} <= set(games[0].keys())


def test_games_endpoint_bounds_and_filters(client):
    games = client.get("/api/stats/games?from_ms=1700000000000&to_ms=1700000050000").json()
    assert len(games) == 1
    games = client.get("/api/stats/games?champion=Kled").json()
    assert len(games) == 1
    assert games[0]["my_champion"] == "Kled"


def test_games_endpoint_opponent_puuid_and_date_params(client):
    games = client.get("/api/stats/games?opp_champion=Darius").json()
    assert games and all(g["opp_champion"] == "Darius" for g in games)
    games = client.get(f"/api/stats/games?puuid={ME}").json()
    assert games and all(g["my_puuid"] == ME for g in games)
    assert client.get("/api/stats/games?range=7d").json() == []  # fixtures are old
    games = client.get("/api/stats/games?from=2023-11-01&to=2023-11-30").json()
    assert len(games) == 2


def test_games_endpoint_rejects_bad_bounds(client):
    assert client.get("/api/stats/games?from_ms=yesterday").status_code == 422


def seed_metrics(client, cs_values):
    """Attach metric rows to the fixture matches (EUW1_* ids ascend)."""
    import os
    from server.metrics import metric_keys
    conn = db.connect(os.environ["LOL_DB_PATH"])
    rows = conn.execute(
        "SELECT match_id FROM participants WHERE puuid=? ORDER BY match_id", (ME,)
    ).fetchall()
    for row, cs in zip(rows, cs_values):
        values = {k: None for k in metric_keys()}
        values.update({"has_challenges": 1, "cs_at_10": cs})
        db.insert_participant_metrics(conn, row["match_id"], ME, values)
    conn.close()


def test_metrics_endpoint_returns_values_and_meta(client):
    seed_metrics(client, [80, 90, 70])
    data = client.get("/api/stats/metrics").json()
    assert data["games"] == 3
    assert data["metrics_games"] == 3
    assert data["metrics"]["cs_at_10"] == pytest.approx(80.0)
    meta = {m["key"]: m for m in data["meta"]}
    assert meta["cs_at_10"]["group"] == "Laning"
    assert meta["time_dead"]["direction"] == -1
    # bounds filtering works like /api/stats/games
    filtered = client.get(
        "/api/stats/metrics?from_ms=1700000000000&to_ms=1700000050000").json()
    assert filtered["games"] == 1


def test_trends_endpoint_buckets_and_meta(client):
    seed_metrics(client, [80, 90, 70])
    data = client.get("/api/stats/trends?bucket=month").json()
    assert [b["bucket"] for b in data["buckets"]] == ["2020-09", "2023-11"]
    assert data["buckets"][1]["games"] == 2
    assert data["buckets"][1]["winrate"] == pytest.approx(0.5)
    assert any(m["key"] == "cs_at_10" for m in data["meta"])
    assert client.get("/api/stats/trends?bucket=decade").status_code == 400
    # default bucket is month
    default = client.get("/api/stats/trends").json()
    assert [b["bucket"] for b in default["buckets"]] == ["2020-09", "2023-11"]


def seed_map_events(client, seeds):
    """seeds: list of (match_id, x, y, timestamp_ms) deaths to attach for ME."""
    import os
    from server.metrics import metric_keys
    conn = db.connect(os.environ["LOL_DB_PATH"])
    for match_id, x, y, ts in seeds:
        values = {k: None for k in metric_keys()}
        values["has_challenges"] = 0
        db.insert_participant_metrics(conn, match_id, ME, values)  # row must pre-exist
        db.replace_map_events(conn, match_id, ME,
                              [{"event_type": "death", "x": x, "y": y, "timestamp_ms": ts}])
    conn.close()


def test_map_events_endpoint_filters_by_champion_and_period(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    rows = conn.execute(
        """SELECT p.match_id, p.champion_name FROM participants p
           JOIN matches m ON m.match_id = p.match_id
           WHERE p.puuid=? ORDER BY m.game_creation_ms""", (ME,)).fetchall()
    conn.close()
    kled_match = next(r["match_id"] for r in rows if r["champion_name"] == "Kled")
    garen_matches = [r["match_id"] for r in rows if r["champion_name"] == "Garen"]

    seed_map_events(client, [
        (kled_match, 500, 500, 30_000),           # game at when=1_600_000_000_000
        (garen_matches[0], 7000, 7000, 400_000),  # game at when=1_700_000_000_000
    ])

    all_events = client.get("/api/stats/map-events").json()["events"]
    assert len(all_events) == 2

    garen_events = client.get("/api/stats/map-events?champion=Garen").json()["events"]
    assert [e["x"] for e in garen_events] == [7000]
    assert garen_events[0]["event_type"] == "death"

    period = client.get("/api/stats/map-events?from_ms=1650000000000").json()["events"]
    assert [e["x"] for e in period] == [7000]  # excludes the Kled game (1_600_000_000_000)

    # role filter: every fixture match is TOP by default (add_match's default)
    assert len(client.get("/api/stats/map-events?role=TOP").json()["events"]) == 2
    assert client.get("/api/stats/map-events?role=JUNGLE").json()["events"] == []


def test_pool_default_and_put_round_trip(client):
    assert client.get("/api/pool").json() == {"main_blind": None, "core": [], "counter": []}
    response = client.put("/api/pool", json={
        "main_blind": "Gwen", "core": ["Kled"], "counter": ["Malphite", "Quinn"]})
    assert response.status_code == 200
    assert client.get("/api/pool").json()["counter"] == ["Malphite", "Quinn"]
    assert client.put("/api/pool", json={"main_blind": "Gwen", "core": "Kled",
                                         "counter": []}).status_code == 400


def test_pool_rejects_unknown_champions(client):
    response = client.put("/api/pool", json={
        "main_blind": "NotAChampion", "core": [], "counter": []})
    assert response.status_code == 400
    assert "NotAChampion" in response.json()["detail"]
    assert client.put("/api/pool", json={
        "main_blind": "Gwen", "core": ["MonkeyKing"], "counter": ["KSante"]}).status_code == 200


def first_two_games(client):
    games = client.get("/api/stats/games").json()
    return games[-1], games[-2]  # oldest first for stable ids


def test_block_add_game_and_listing(client):
    game = client.get("/api/stats/games").json()[0]
    response = client.post("/api/blocks/games",
                           json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    assert response.status_code == 200
    block_id = response.json()["block_id"]
    blocks = client.get("/api/blocks").json()["blocks"]
    assert blocks[0]["id"] == block_id
    assert blocks[0]["complete"] is False
    entry = blocks[0]["games"][0]
    assert entry["my_champion"] in ("Garen", "Kled")
    assert entry["account"] == "PlayerOne"
    assert "opp_champion" in entry


def test_block_add_duplicate_409_names_block(client):
    game = client.get("/api/stats/games").json()[0]
    payload = {"match_id": game["match_id"], "puuid": game["my_puuid"]}
    block_id = client.post("/api/blocks/games", json=payload).json()["block_id"]
    response = client.post("/api/blocks/games", json=payload)
    assert response.status_code == 409
    assert str(block_id) in response.json()["detail"]


def test_block_add_unknown_pair_404(client):
    assert client.post("/api/blocks/games",
                       json={"match_id": "EUW1_nope", "puuid": ME}).status_code == 404


def test_block_patch_and_deletes(client):
    game = client.get("/api/stats/games").json()[0]
    block_id = client.post("/api/blocks/games", json={
        "match_id": game["match_id"], "puuid": game["my_puuid"]}).json()["block_id"]
    assert client.patch(f"/api/blocks/{block_id}",
                        json={"title": "T", "learnings": "## L"}).status_code == 200
    assert client.patch(f"/api/blocks/{block_id}", json={}).status_code == 400
    assert client.patch("/api/blocks/999", json={"title": "x"}).status_code == 404
    blocks = client.get("/api/blocks").json()["blocks"]
    assert blocks[0]["title"] == "T"
    entry_id = blocks[0]["games"][0]["entry_id"]
    assert client.patch(f"/api/blocks/games/{entry_id}",
                        json={"notes": "kept tempo"}).status_code == 200
    assert client.patch("/api/blocks/games/999", json={"notes": "x"}).status_code == 404
    assert client.delete(f"/api/blocks/games/{entry_id}").status_code == 200
    assert client.delete(f"/api/blocks/games/{entry_id}").status_code == 404
    assert client.delete(f"/api/blocks/{block_id}").status_code == 200
    assert client.delete(f"/api/blocks/{block_id}").status_code == 404


def test_block_game_manual_lane_result_api(client):
    game = client.get("/api/stats/games").json()[0]
    client.post("/api/blocks/games",
                json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    entry_id = client.get("/api/blocks").json()["blocks"][0]["games"][0]["entry_id"]

    assert client.patch(f"/api/blocks/games/{entry_id}",
                        json={"lane_result_7": "lost", "lane_result_14": "won"}
                        ).status_code == 200
    stored = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    assert (stored["lane_result_7"], stored["lane_result_14"]) == ("lost", "won")

    # a mark not in the body is left alone, so the two never clobber each other
    client.patch(f"/api/blocks/games/{entry_id}", json={"lane_result_7": None})
    stored = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    assert (stored["lane_result_7"], stored["lane_result_14"]) == (None, "won")

    # ...and neither clobbers the notes
    client.patch(f"/api/blocks/games/{entry_id}", json={"notes": "dove at 6"})
    client.patch(f"/api/blocks/games/{entry_id}", json={"lane_result_14": "stomp"})
    stored = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    assert stored["notes"] == "dove at 6"
    assert stored["lane_result_14"] == "stomp"

    assert client.patch(f"/api/blocks/games/{entry_id}",
                        json={"lane_result_7": "sideways"}).status_code == 400
    assert client.patch(f"/api/blocks/games/{entry_id}", json={}).status_code == 400
    assert client.patch("/api/blocks/games/999",
                        json={"lane_result_7": "won"}).status_code == 404


def test_block_game_weakside_api(client):
    game = client.get("/api/stats/games").json()[0]
    client.post("/api/blocks/games",
                json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    entry_id = client.get("/api/blocks").json()["blocks"][0]["games"][0]["entry_id"]
    stored = lambda: client.get("/api/blocks").json()["blocks"][0]["games"][0]  # noqa: E731
    assert stored()["weakside"] is None
    assert client.patch(f"/api/blocks/games/{entry_id}",
                        json={"weakside": True}).status_code == 200
    assert stored()["weakside"] == 1
    client.patch(f"/api/blocks/games/{entry_id}", json={"weakside": None})
    assert stored()["weakside"] is None


def _disable_block_gap(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.set_settings(conn, {"block_gap_hours": "0"})
    conn.close()


def test_blocks_expose_parsed_pool_snapshot(client):
    _disable_block_gap(client)  # fixture games are years apart in game time
    client.put("/api/pool", json={"main_blind": "Gwen", "core": ["Kled"], "counter": []})
    games = client.get("/api/stats/games").json()[:3]
    for game in games:
        client.post("/api/blocks/games",
                    json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    block = client.get("/api/blocks").json()["blocks"][0]
    assert block["complete"] is True
    assert block["pool"] == {"main_blind": "Gwen", "core": ["Kled"], "counter": []}


def test_pool_save_stamps_completed_current_block_without_snapshot(client, monkeypatch):
    import os
    _disable_block_gap(client)  # fixture games are years apart in game time
    # complete a block with an empty pool (no snapshot content of value)
    conn = db.connect(os.environ["LOL_DB_PATH"])
    games = client.get("/api/stats/games").json()[:3]
    for game in games:
        client.post("/api/blocks/games",
                    json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    # wipe the snapshot to simulate a block completed before the feature existed
    conn.execute("UPDATE blocks SET pool_snapshot=NULL")
    conn.commit()
    conn.close()
    client.put("/api/pool", json={"main_blind": "Gwen", "core": [], "counter": []})
    block = client.get("/api/blocks").json()["blocks"][0]
    assert block["pool"]["main_blind"] == "Gwen"


def test_settings_unconfigured_by_default(client):
    data = client.get("/api/settings").json()
    assert data["configured"] is False
    assert data["riot_api_key"] == ""
    assert data["accounts"] == []
    assert data["platform"] == "euw1"
    assert "euw1" in data["platforms"] and "na1" in data["platforms"]


def test_settings_put_round_trip(client):
    response = client.put("/api/settings", json={
        "riot_api_key": "RGAPI-new", "accounts": ["Foo#BAR", "Baz#EUW"], "platform": "NA1"})
    assert response.status_code == 200
    data = client.get("/api/settings").json()
    assert data["configured"] is True
    assert data["source"] == "db"
    assert data["accounts"] == ["Foo#BAR", "Baz#EUW"]
    assert data["platform"] == "na1"


def test_settings_put_validation(client):
    assert client.put("/api/settings", json={
        "riot_api_key": "", "accounts": ["A#B"], "platform": "euw1"}).status_code == 400
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["NoTag"], "platform": "euw1"}).status_code == 400
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": [], "platform": "euw1"}).status_code == 400
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "moon1"}).status_code == 400


def test_single_game_metrics_endpoint(client):
    seed_metrics(client, [80, 90, 70])
    game = client.get("/api/stats/games").json()[0]
    data = client.get(
        f"/api/stats/games/metrics?match_id={game['match_id']}&puuid={game['my_puuid']}").json()
    assert data["metrics"]["cs_at_10"] in (80, 90, 70)
    assert any(m["key"] == "cs_at_10" for m in data["meta"])
    assert client.get(
        "/api/stats/games/metrics?match_id=EUW1_nope&puuid=x").status_code == 404


def test_game_curve_endpoint(client):
    import os
    game = client.get("/api/stats/summary").json()["recent"][0]
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.insert_frame_series(conn, [
        {"match_id": game["match_id"], "puuid": game["my_puuid"], "minute": 0,
         "cs": 0, "xp": 0, "gold": 500, "level": 1},
        {"match_id": game["match_id"], "puuid": game["my_puuid"], "minute": 7,
         "cs": 55, "xp": 3200, "gold": 2600, "level": 6},
        {"match_id": game["match_id"], "puuid": game["opp_puuid"], "minute": 7,
         "cs": 40, "xp": 2800, "gold": 2100, "level": 5},
    ])
    conn.close()
    data = client.get(
        f"/api/stats/game-curve?match_id={game['match_id']}&puuid={game['my_puuid']}"
        f"&opp_puuid={game['opp_puuid']}").json()
    assert data["minutes"] == [0, 7]
    assert data["me"]["cs"] == [0, 55]
    assert data["opp"]["cs"] == [40]  # opp only has the 7-min frame recorded
    # without opp_puuid, opp is simply absent
    no_opp = client.get(
        f"/api/stats/game-curve?match_id={game['match_id']}&puuid={game['my_puuid']}").json()
    assert no_opp["opp"] is None
    assert client.get(
        "/api/stats/game-curve?match_id=EUW1_nope&puuid=x").status_code == 404


def test_settings_auto_crawl_round_trip_and_default(client):
    data = client.get("/api/settings").json()
    assert data["auto_crawl_hours"] == 3      # default: every few hours
    assert data["last_crawl_ms"] is None
    response = client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "auto_crawl_hours": 12})
    assert response.status_code == 200
    assert client.get("/api/settings").json()["auto_crawl_hours"] == 12
    # 0 disables; negatives and junk rejected
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "auto_crawl_hours": 0}).status_code == 200
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "auto_crawl_hours": -2}).status_code == 400
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "auto_crawl_hours": "soon"}).status_code == 400


def test_settings_hidden_views_round_trip(client):
    assert client.get("/api/settings").json()["hidden_views"] == []
    response = client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "hidden_views": ["overview", "trends"]})
    assert response.status_code == 200
    assert client.get("/api/settings").json()["hidden_views"] == ["overview", "trends"]
    assert client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1",
        "hidden_views": ["settings"]}).status_code == 400


def test_session_and_block_ranks_exposed_parsed(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    conn.execute("UPDATE players SET solo_tier='PLATINUM', solo_division='II', solo_lp=45")
    conn.commit()
    conn.close()
    client.post("/api/sessions", json={"date": "2026-07-05"})
    session = client.get("/api/sessions").json()[0]
    assert session["start_ranks"][0]["tier"] == "PLATINUM"
    game = client.get("/api/stats/games").json()[0]
    client.post("/api/blocks/games", json={"match_id": game["match_id"],
                                           "puuid": game["my_puuid"]})
    block = client.get("/api/blocks").json()["blocks"][0]
    assert block["start_ranks"][0]["lp"] == 45
    assert block["end_ranks"] is None


def test_version_endpoint(client):
    data = client.get("/api/version").json()
    assert data["version"].count(".") == 2  # semver from the VERSION file
    assert data["repo"] == "Muhwu/coach-potato"


def seed_block(client):
    _disable_block_gap(client)  # fixture games are years apart in game time
    games = client.get("/api/stats/games").json()[:2]
    for game in games:
        client.post("/api/blocks/games",
                    json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    blocks = client.get("/api/blocks").json()["blocks"]
    block = blocks[0]
    client.patch(f"/api/blocks/{block['id']}",
                 json={"title": "Fundamentals", "learnings": "- freeze more"})
    client.patch(f"/api/blocks/games/{block['games'][0]['entry_id']}",
                 json={"notes": "good tempo"})
    return block["id"]


def test_blocks_export_markdown(client):
    seed_block(client)
    response = client.get("/api/blocks/export.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "block-learnings.md" in response.headers["content-disposition"]
    body = response.text
    assert body.startswith("# Block Learnings")
    assert "## Block #1 — Fundamentals" in body
    assert "- freeze more" in body
    assert "good tempo" in body
    assert "Garen" in body or "Kled" in body  # hydrated matchup line


def test_blocks_export_single_block(client):
    seed_block(client)  # block 1: two games
    game = client.get("/api/stats/games").json()[2]
    client.post("/api/blocks/games",
                json={"match_id": game["match_id"], "puuid": game["my_puuid"]})  # fills block 1
    game4 = client.get("/api/stats/games").json()  # any game already used? use sessions...
    body = client.get("/api/blocks/export.md?block_id=1").text
    assert "## Block #1 — Fundamentals" in body
    csv_lines = client.get("/api/blocks/export.csv?block_id=1").text.strip().splitlines()
    assert len(csv_lines) == 4  # header + 3 games
    assert client.get("/api/blocks/export.md?block_id=99").status_code == 404


def test_blocks_export_csv(client):
    seed_block(client)
    response = client.get("/api/blocks/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("block,title,date,account,champion,opponent,result")
    assert len(lines) == 3  # header + 2 games
    assert "Fundamentals" in lines[1]
    assert "good tempo" in response.text


def test_index_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_rank_history_endpoint(client, tmp_path, monkeypatch):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.record_rank_history(conn, ME, "GOLD", "II", 40, 1_700_000_000_000)
    db.record_rank_history(conn, ME, "GOLD", "I", 5, 1_700_100_000_000)
    db.add_session(conn, "2026-07-01", "wave management")
    conn.close()
    data = client.get("/api/stats/rank-history").json()
    assert len(data["series"]) == 1
    series = data["series"][0]
    assert series["account"] == "PlayerOne#EUW"
    real = [p for p in series["points"] if not p["estimated"]]
    assert [p["value"] for p in real] == [1440, 1505]
    # the ranked loss between the snapshots becomes a -20 estimated point
    estimated = [p for p in series["points"] if p["estimated"]]
    assert [(p["t"], p["value"]) for p in estimated] == [(1_700_000_100_000, 1420)]
    assert data["sessions"] == [{"date": "2026-07-01", "title": "wave management"}]


CONQ_PAGE = {
    "label": "Standard", "primary_tree": "Precision", "keystone": "Conqueror",
    "primary_runes": ["Triumph", "Legend: Alacrity", "Last Stand"],
    "secondary_tree": "Resolve", "secondary_runes": ["Bone Plating", "Overgrowth"],
    "shards": ["Adaptive Force", "Adaptive Force", "Health"],
}
GRASP_PAGE = {
    "label": "vs poke", "primary_tree": "Resolve", "keystone": "Grasp of the Undying",
    "primary_runes": ["Demolish", "Second Wind", "Overgrowth"],
    "secondary_tree": "Inspiration", "secondary_runes": ["Biscuit Delivery", "Cosmic Insight"],
    "shards": ["Health", "Armor", "Health"],
}


def test_matchup_notes_endpoints(client):
    assert client.get("/api/matchups/notes?my_champion=Gwen").json() == {}
    # a matchup can carry more than one rune page (e.g. alternatives being tested)
    r = client.put("/api/matchups/notes/Gwen/Darius", json={
        "notes": "- respect level 2", "runes": [CONQ_PAGE, GRASP_PAGE], "patch_version": "14.14"})
    assert r.status_code == 200
    assert client.get("/api/matchups/notes?my_champion=Gwen").json() == {"Darius": {
        "notes": "- respect level 2", "runes": [CONQ_PAGE, GRASP_PAGE],
        "patch_version": "14.14", "skill_order": []}}
    # a different "my champion" has its own, independent guide
    assert client.get("/api/matchups/notes?my_champion=Camille").json() == {}
    assert client.get("/api/matchups/notes").status_code == 422  # my_champion required
    client.put("/api/matchups/notes/Gwen/Darius", json={
        "notes": "", "runes": [], "patch_version": ""})
    assert client.get("/api/matchups/notes?my_champion=Gwen").json() == {}  # all-blank deletes
    assert client.put("/api/matchups/notes/Gwen/NotAChamp",
                      json={"notes": "x"}).status_code == 400
    assert client.put("/api/matchups/notes/NotAChamp/Darius",
                      json={"notes": "x"}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius", json={}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"runes": [{"keystone": "Not A Rune"}]}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"runes": [{"primary_tree": "Not A Tree"}]}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"runes": [{"shards": ["Not A Shard"]}]}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"runes": "not-a-list"}).status_code == 400


def _put_settings(client, **extra):
    return client.put("/api/settings", json={
        "riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1", **extra})


def test_hide_my_rank_setting_round_trip(client):
    assert client.get("/api/settings").json()["hide_my_rank"] is False
    assert _put_settings(client, hide_my_rank=True).status_code == 200
    assert client.get("/api/settings").json()["hide_my_rank"] is True
    assert _put_settings(client, hide_my_rank="yes").status_code == 400


def test_hide_my_rank_redacts_all_endpoints(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    conn.execute("UPDATE players SET solo_tier='GOLD', solo_division='II', solo_lp=40,"
                 " rank_fetched_at_ms=1000 WHERE puuid=?", (ME,))
    conn.commit()
    db.record_rank_history(conn, ME, "GOLD", "II", 40, 1_700_000_000_000)
    db.add_session(conn, "2026-07-01", "t")  # captures start_ranks
    conn.close()
    assert _put_settings(client, hide_my_rank=True).status_code == 200

    player = client.get("/api/players").json()[0]
    assert player["solo_tier"] is None and player["solo_lp"] is None
    assert client.get("/api/sessions").json()[0]["start_ranks"] is None
    history = client.get("/api/stats/rank-history").json()
    assert history["series"][0]["points"] == []
    segments = client.get("/api/stats/progress").json()
    assert all(s["start_ranks"] is None for s in segments)

    assert _put_settings(client, hide_my_rank=False).status_code == 200
    assert client.get("/api/players").json()[0]["solo_tier"] == "GOLD"
    assert client.get("/api/sessions").json()[0]["start_ranks"] is not None
    assert client.get("/api/stats/rank-history").json()["series"][0]["points"] != []


def test_block_game_notes_endpoint(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    # the fixture's two Garen-vs-Darius games go into a block, one with notes
    m1, m2 = [r["match_id"] for r in conn.execute(
        """SELECT p.match_id FROM participants p
           JOIN matches m ON m.match_id = p.match_id
           WHERE p.puuid=? AND p.champion_name='Garen'
           ORDER BY m.game_creation_ms""", (ME,))]
    db.add_game_to_block(conn, m1, ME)
    db.add_game_to_block(conn, m2, ME)
    entry = conn.execute("SELECT id FROM block_games WHERE match_id=?", (m1,)).fetchone()
    db.update_block_game(conn, entry["id"], "punished his E cooldown")
    db.update_block(conn, 1, title="lane control", learnings="- track ghost cd")
    conn.close()
    notes = client.get("/api/blocks/game-notes?opp_champion=Darius").json()
    assert len(notes) == 1  # the note-less game is skipped
    n = notes[0]
    assert n["notes"] == "punished his E cooldown"
    assert n["block_id"] == 1 and n["block_title"] == "lane control"
    assert n["block_learnings"] == "- track ghost cd"
    assert n["my_champion"] == "Garen" and n["opp_champion"] == "Darius"
    assert n["match_id"] == m1 and n["account"] == "PlayerOne"
    assert client.get("/api/blocks/game-notes?opp_champion=Teemo").json() == []


def test_stats_endpoints_accept_multi_and_no_puuid(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.upsert_player(conn, "smurf-1", "Smurf", "EUW", is_tracked=True)
    from tests.test_stats import add_match
    add_match(conn, my_champ="Sett", opp_champ="Darius", win=True,
              when=1_700_000_200_000, puuid="smurf-1")
    conn.close()
    # no puuid = all tracked accounts combined
    s = client.get("/api/stats/summary").json()
    assert s["games"] == 4
    # repeated puuid params scope to that subset
    s = client.get(f"/api/stats/summary?puuid={ME}&puuid=smurf-1").json()
    assert s["games"] == 4
    assert all("my_puuid" in g for g in s["recent"])
    s = client.get("/api/stats/summary?puuid=smurf-1").json()
    assert s["games"] == 1
    rows = client.get("/api/stats/matchups?puuid=smurf-1").json()
    assert [r["opp_champion"] for r in rows] == ["Darius"]
    opts = client.get("/api/filters").json()  # union across all tracked
    assert set(opts["champions"]) == {"Garen", "Kled", "Sett"}
    opts = client.get("/api/filters?puuid=smurf-1").json()
    assert opts["champions"] == ["Sett"]
    # progress/metrics/trends scope too
    games = client.get("/api/stats/games?puuid=smurf-1").json()
    assert len(games) == 1 and games[0]["account"] == "Smurf"


def test_close_block_endpoint(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    m1, _ = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM participants WHERE puuid=? ORDER BY match_id", (ME,))][:2], None
    db.add_game_to_block(conn, m1[0], ME)
    conn.close()
    assert client.post("/api/blocks/999/close").status_code == 404
    assert client.post("/api/blocks/1/close").json() == {"closed": True}
    blocks = client.get("/api/blocks").json()["blocks"]
    assert blocks[0]["closed"] is True and blocks[0]["complete"] is True
    assert client.post("/api/blocks/1/close").status_code == 409


def test_matchup_notes_accept_match_v5_champion_spelling(client):
    # match-v5 says FiddleSticks; DDragon says Fiddlesticks — both must save
    assert client.put("/api/matchups/notes/Gwen/FiddleSticks",
                      json={"notes": "ban worthy"}).status_code == 200
    assert client.get("/api/matchups/notes?my_champion=Gwen").json()["FiddleSticks"]["notes"] == "ban worthy"


def test_comparison_players_and_settings(client):
    base = {"riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1"}
    r = client.put("/api/settings", json={**base, "enable_player_comparison": True})
    assert r.status_code == 200
    assert r.json()["enable_player_comparison"] is True
    # comparison endpoints without any players
    body = client.get("/api/comparison-players").json()
    assert body["players"] == [] and body["max"] == db.MAX_COMPARISON_PLAYERS
    assert body["fetching"]["running"] is False  # background-fetch status
    # comparison off -> empty player list even if some exist
    client.put("/api/settings", json={**base, "enable_player_comparison": False})
    body = client.get("/api/comparison",
                      params={"my_champion": "Gwen", "opp_champion": "Darius"}).json()
    assert body["players"] == []
    assert body["you"]["scoped"] is not None  # your own column stays either way
    # PATCH validates the enabled flag
    assert client.patch("/api/comparison-players/xyz", json={"enabled": "no"}).status_code == 400
def test_reflection_endpoints(client):
    assert client.get("/api/reflections?match_id=EUW1_1&puuid=me").json() == {
        "tags": [], "note": ""}
    r = client.put("/api/reflections/EUW1_1/me", json={
        "tags": ["bad TP", "tilted"], "note": "- forced a bad TP"})
    assert r.status_code == 200
    assert client.get("/api/reflections?match_id=EUW1_1&puuid=me").json() == {
        "tags": ["bad TP", "tilted"], "note": "- forced a bad TP"}
    # tags-only update never clobbers the stored note
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"tags": ["bad TP"]}).status_code == 200
    got = client.get("/api/reflections?match_id=EUW1_1&puuid=me").json()
    assert got == {"tags": ["bad TP"], "note": "- forced a bad TP"}
    # note-only update never clobbers the stored tags
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"note": "updated"}).status_code == 200
    got = client.get("/api/reflections?match_id=EUW1_1&puuid=me").json()
    assert got == {"tags": ["bad TP"], "note": "updated"}
    # a different game/player is independent
    assert client.get("/api/reflections?match_id=EUW1_2&puuid=me").json() == {
        "tags": [], "note": ""}
    assert client.get("/api/reflections?match_id=EUW1_1&puuid=opp").json() == {
        "tags": [], "note": ""}
    # blanking both clears the row
    client.put("/api/reflections/EUW1_1/me", json={"tags": [], "note": ""})
    assert client.get("/api/reflections?match_id=EUW1_1&puuid=me").json() == {
        "tags": [], "note": ""}
    # validation
    assert client.get("/api/reflections").status_code == 422  # match_id/puuid required
    assert client.put("/api/reflections/EUW1_1/me", json={}).status_code == 400
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"tags": "not-a-list"}).status_code == 400
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"tags": [""]}).status_code == 400  # empty tag string
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"tags": ["x" * 41]}).status_code == 400  # too long
    assert client.put("/api/reflections/EUW1_1/me",
                      json={"tags": ["ok"] * 21}).status_code == 400  # too many


def test_champion_general_notes_endpoints(client):
    assert client.get("/api/champions/notes/Gwen").json() == {"notes": "", "runes": []}
    r = client.put("/api/champions/notes/Gwen", json={"notes": "- always take Conqueror"})
    assert r.status_code == 200
    assert client.get("/api/champions/notes/Gwen").json() == {
        "notes": "- always take Conqueror", "runes": []}
    client.put("/api/champions/notes/Gwen", json={"notes": ""})  # blank deletes
    assert client.get("/api/champions/notes/Gwen").json() == {"notes": "", "runes": []}
    assert client.put("/api/champions/notes/NotAChamp", json={"notes": "x"}).status_code == 400
    assert client.put("/api/champions/notes/Gwen", json={}).status_code == 400


def test_delete_account_purges_data_and_removes_from_settings(client):
    import json
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.set_settings(conn, {"accounts": json.dumps(["PlayerOne#EUW", "Other#EUW"])})
    assert conn.execute("SELECT COUNT(*) c FROM participants WHERE puuid=?",
                        (ME,)).fetchone()["c"] > 0
    conn.close()
    r = client.request("DELETE", "/api/accounts", json={"account": "playerone#euw"})  # case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["players_deleted"] == 1
    assert body["accounts"] == ["Other#EUW"]
    conn = db.connect(os.environ["LOL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) c FROM participants WHERE puuid=?",
                        (ME,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM players WHERE puuid=?",
                        (ME,)).fetchone()["c"] == 0
    conn.close()
    assert client.request("DELETE", "/api/accounts", json={"account": "notag"}).status_code == 400


def test_champion_general_runes_endpoints(client):
    # runes_mode='general' stores champion-level rune pages alongside notes
    page = {"label": "Standard", "primary_tree": "Precision", "keystone": "Conqueror",
            "primary_runes": ["Triumph", "", ""], "secondary_tree": "Resolve",
            "secondary_runes": ["", ""], "shards": ["", "", ""]}
    assert client.put("/api/champions/notes/Gwen", json={"runes": [page]}).status_code == 200
    got = client.get("/api/champions/notes/Gwen").json()
    assert got["notes"] == "" and len(got["runes"]) == 1
    assert got["runes"][0]["keystone"] == "Conqueror"
    # a partial notes-only write must not wipe the general runes
    client.put("/api/champions/notes/Gwen", json={"notes": "matchup-agnostic tips"})
    got = client.get("/api/champions/notes/Gwen").json()
    assert got["notes"] == "matchup-agnostic tips" and len(got["runes"]) == 1
    # clearing runes with notes still present keeps the row
    client.put("/api/champions/notes/Gwen", json={"runes": []})
    got = client.get("/api/champions/notes/Gwen").json()
    assert got["runes"] == [] and got["notes"] == "matchup-agnostic tips"
    # a bad rune tree is rejected
    bad = {**page, "primary_tree": "NotATree"}
    assert client.put("/api/champions/notes/Gwen", json={"runes": [bad]}).status_code == 400


def test_runes_mode_setting_round_trips(client):
    base = {"riot_api_key": "k", "accounts": ["A#B"], "platform": "euw1"}
    assert client.get("/api/settings").json()["runes_mode"] == "matchup"
    r = client.put("/api/settings", json={**base, "runes_mode": "general"})
    assert r.status_code == 200 and r.json()["runes_mode"] == "general"
    assert client.put("/api/settings", json={**base, "runes_mode": "nope"}).status_code == 400


def test_champion_item_build_endpoints(client):
    assert client.get("/api/champions/item-build/Gwen").json() == {"core": [], "situational": []}
    body = {"core": ["Riftmaker", "Nashor's Tooth"],
            "situational": [{"label": "vs heavy AP", "items": ["Zhonya's Hourglass"]}]}
    assert client.put("/api/champions/item-build/Gwen", json=body).status_code == 200
    assert client.get("/api/champions/item-build/Gwen").json() == body
    assert client.put("/api/champions/item-build/Gwen", json={"core": [], "situational": []}).status_code == 200
    assert client.get("/api/champions/item-build/Gwen").json() == {"core": [], "situational": []}


def test_champion_item_build_validation(client):
    assert client.put("/api/champions/item-build/NotAChamp", json={"core": []}).status_code == 400
    assert client.put("/api/champions/item-build/Gwen",
                       json={"core": ["A"] * 7}).status_code == 400  # over MAX_CORE_ITEMS
    assert client.put("/api/champions/item-build/Gwen",
                       json={"core": [123]}).status_code == 400  # not strings
    assert client.put("/api/champions/item-build/Gwen",
                       json={"situational": [{"label": "", "items": ["A"]}]}).status_code == 400  # no label
    assert client.put("/api/champions/item-build/Gwen",
                       json={"situational": [{"label": "x", "items": ["A"] * 6}]}).status_code == 400  # over 5
    assert client.put("/api/champions/item-build/Gwen",
                       json={"situational": [{"label": "x"}] * 13}).status_code == 400  # over 12 sections


def _seed_champ_guide(client):
    client.put("/api/champions/notes/Gwen", json={"notes": "general Gwen tips"})
    client.put("/api/champions/item-build/Gwen", json={
        "core": ["Riftmaker", "Nashor's Tooth"],
        "situational": [{"label": "vs heavy AP", "items": ["Zhonya's Hourglass"]}]})
    client.put("/api/matchups/notes/Gwen/Darius", json={
        "notes": "respect level 2", "runes": [CONQ_PAGE], "patch_version": "14.14"})
    client.put("/api/matchups/notes/Gwen/Renekton", json={"notes": "easy lane"})


def test_champ_guide_export_plain(client):
    _seed_champ_guide(client)
    r = client.post("/api/matchups/notes/export", json={"my_champion": "Gwen"})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "champ-guide-gwen.json" in r.headers["content-disposition"]
    data = r.json()
    assert data["kind"] == "champ-guide-export"
    assert data["my_champion"] == "Gwen"
    assert data["encrypted"] is False
    assert data["general_notes"] == "general Gwen tips"
    assert data["item_build"]["core"] == ["Riftmaker", "Nashor's Tooth"]
    assert data["guide"]["Darius"]["notes"] == "respect level 2"
    assert data["guide"]["Renekton"]["notes"] == "easy lane"


def test_champ_guide_export_encrypted_hides_plaintext(client):
    _seed_champ_guide(client)
    r = client.post("/api/matchups/notes/export",
                     json={"my_champion": "Gwen", "password": "hunter2"})
    data = r.json()
    assert data["encrypted"] is True
    assert "guide" not in data and "general_notes" not in data
    assert "ciphertext" in data and "salt" in data
    raw = r.text
    assert "respect level 2" not in raw  # plaintext notes must not leak into the encrypted file


def test_champ_guide_export_pdf(client, monkeypatch):
    import httpx as httpx_module
    from server import pdf_export as pdf_export_module

    def fake_get(url, timeout=5.0):
        return httpx_module.Response(200, content=b"", request=httpx_module.Request("GET", url))
    monkeypatch.setattr(pdf_export_module.httpx, "get", fake_get)

    _seed_champ_guide(client)
    r = client.get("/api/matchups/notes/export.pdf", params={"my_champion": "Gwen"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "champ-guide-gwen.pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")


def test_champ_guide_export_pdf_requires_my_champion(client):
    assert client.get("/api/matchups/notes/export.pdf").status_code == 422  # missing query param
    assert client.get("/api/matchups/notes/export.pdf", params={"my_champion": ""}).status_code == 400


def test_champ_guide_import_plain_round_trip(client):
    _seed_champ_guide(client)
    export = client.post("/api/matchups/notes/export", json={"my_champion": "Gwen"}).json()
    # import into a fresh champion to prove the round trip reproduces the data
    export["my_champion"] = "Camille"
    preview = client.post("/api/matchups/notes/import/preview", json={"data": export}).json()
    assert preview["my_champion"] == "Camille"
    assert sorted(preview["opponents"]) == ["Darius", "Renekton"]
    assert preview["will_overwrite"] == []
    assert preview["has_general_notes"] is True
    assert preview["has_item_build"] is True
    r = client.post("/api/matchups/notes/import", json={"data": export})
    assert r.status_code == 200
    assert r.json() == {"imported": 2}
    assert client.get("/api/champions/notes/Camille").json()["notes"] == "general Gwen tips"
    assert client.get("/api/champions/item-build/Camille").json()["core"] == ["Riftmaker", "Nashor's Tooth"]
    guide = client.get("/api/matchups/notes?my_champion=Camille").json()
    assert guide["Darius"]["runes"] == [CONQ_PAGE]
    assert guide["Renekton"]["notes"] == "easy lane"


def test_champ_guide_import_detects_overwrites(client):
    _seed_champ_guide(client)
    export = client.post("/api/matchups/notes/export", json={"my_champion": "Gwen"}).json()
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "already had different notes"})
    preview = client.post("/api/matchups/notes/import/preview", json={"data": export}).json()
    # both matchups already existed for Gwen before the import (from the seed)
    assert sorted(preview["will_overwrite"]) == ["Darius", "Renekton"]
    client.post("/api/matchups/notes/import", json={"data": export})
    assert client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]["notes"] == "respect level 2"


def test_champ_guide_import_encrypted_requires_correct_password(client):
    _seed_champ_guide(client)
    export = client.post("/api/matchups/notes/export",
                          json={"my_champion": "Gwen", "password": "hunter2"}).json()
    export["my_champion"] = "Camille"
    assert client.post("/api/matchups/notes/import/preview",
                       json={"data": export}).status_code == 401  # no password
    assert client.post("/api/matchups/notes/import/preview",
                       json={"data": export, "password": "wrong"}).status_code == 401
    r = client.post("/api/matchups/notes/import/preview",
                    json={"data": export, "password": "hunter2"})
    assert r.status_code == 200
    assert sorted(r.json()["opponents"]) == ["Darius", "Renekton"]
    assert client.post("/api/matchups/notes/import",
                       json={"data": export, "password": "hunter2"}).json() == {"imported": 2}
    assert client.get("/api/matchups/notes?my_champion=Camille").json()["Darius"]["runes"] == [CONQ_PAGE]


def test_rune_page_with_empty_slots_saves(client):
    # the picker sends "" placeholders for unfilled slots — a half-built
    # page must save, not 400 (regression: "not a rune: " on every partial page)
    partial = {"label": "", "primary_tree": "Precision", "keystone": "Conqueror",
               "primary_runes": ["Triumph", "", ""], "secondary_tree": "",
               "secondary_runes": [], "shards": ["", "", ""]}
    r = client.put("/api/matchups/notes/Gwen/Darius", json={"runes": [partial]})
    assert r.status_code == 200
    assert client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]["runes"] == [partial]
    # real bad names still rejected
    assert client.put("/api/matchups/notes/Gwen/Darius", json={
        "runes": [{**partial, "primary_runes": ["Fake Rune", "", ""]}]}).status_code == 400
    assert client.put("/api/matchups/notes/Gwen/Darius", json={
        "runes": [{**partial, "shards": ["Fake Shard", "", ""]}]}).status_code == 400


def test_skill_order_endpoint_roundtrip_and_partial_update(client):
    order = ["Q", "W", "E", "Q", "Q", "R", "Q", "W", "Q", "W", "R", "W", "W",
             "E", "E", "R", "E", "E"]
    # build saves alone (cooldown popup) without touching other fields
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "keep these notes"})
    r = client.put("/api/matchups/notes/Gwen/Darius", json={"skill_order": order})
    assert r.status_code == 200
    guide = client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]
    assert guide["skill_order"] == order
    assert guide["notes"] == "keep these notes"
    # editor-style save (no skill_order key) keeps the saved build
    client.put("/api/matchups/notes/Gwen/Darius",
               json={"notes": "edited", "runes": [], "patch_version": ""})
    guide = client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]
    assert guide["skill_order"] == order
    assert guide["notes"] == "edited"
    # partial grids are fine; sparse levels allowed
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"skill_order": ["Q", "", "W"]}).status_code == 200


def test_skill_order_validation(client):
    put = lambda so: client.put("/api/matchups/notes/Gwen/Darius",
                                json={"skill_order": so}).status_code
    assert put("QWER") == 400                       # not a list
    assert put(["X"]) == 400                        # unknown ability
    assert put([""] * 19) == 400                    # more than 18 levels
    assert put(["Q", "Q"]) == 400                   # Q rank 2 needs level 3
    assert put(["R"]) == 400                        # R needs level 6
    assert put([""] * 5 + ["R", "R"]) == 400        # R rank 2 needs level 11
    assert put(["Q", "W", "Q", "Q"]) == 400         # Q rank 3 needs level 5
    assert put(["Q", "W", "E", "Q", "Q", "R", "Q", "W", "Q", "W", "R", "W",
                "W", "E", "E", "R", "E", "E", ]) == 200  # a legal full build
    # 6 points in one basic ability
    assert put(["Q", "W", "Q", "W", "Q", "R", "Q", "W", "Q", "W", "R", "Q"]) == 400


def test_champ_guide_import_rejects_non_export_file(client):
    assert client.post("/api/matchups/notes/import",
                       json={"data": {"not": "an export"}}).status_code == 400
    assert client.post("/api/matchups/notes/import", json={}).status_code == 400


def test_champ_guide_import_validates_payload_shape(client):
    # a hand-edited export with bad runes/entries must 400 (never 500 or
    # store garbage) — import applies the same rune checks as the PUT
    def export_with(guide):
        return {"app": "coach-potato", "kind": "champ-guide-export", "version": 1,
                "my_champion": "Gwen", "encrypted": False,
                "general_notes": "", "guide": guide}
    for bad_guide in (
        "not-an-object",
        {"Darius": "not-an-entry"},
        {"Darius": {"runes": "not-a-list"}},
        {"Darius": {"runes": ["not-a-page"]}},
        {"Darius": {"runes": [{"keystone": "Not A Rune"}]}},
    ):
        body = {"data": export_with(bad_guide)}
        assert client.post("/api/matchups/notes/import", json=body).status_code == 400
        assert client.post("/api/matchups/notes/import/preview", json=body).status_code == 400


def test_champ_guide_import_caps_pbkdf2_iterations(client):
    _seed_champ_guide(client)
    export = client.post("/api/matchups/notes/export",
                          json={"my_champion": "Gwen", "password": "hunter2"}).json()
    export["iterations"] = 10_000_000_000  # crafted file must not pin the CPU
    r = client.post("/api/matchups/notes/import/preview",
                    json={"data": export, "password": "hunter2"})
    assert r.status_code == 401


def test_champ_guide_export_validates_champion(client):
    assert client.post("/api/matchups/notes/export",
                       json={"my_champion": "NotAChamp"}).status_code == 400
    assert client.get("/api/matchups/notes/export.pdf",
                      params={"my_champion": "NotAChamp"}).status_code == 400


def _seed_legacy_notes(client):
    # rows exactly as the champ-guide migration leaves them: my_champion=''
    conn = db.connect(app_module.get_db_path())
    db.set_matchup_note(conn, "", "Darius", notes="- care ghost timings")
    db.set_matchup_note(conn, "", "Teemo", notes="ban it")
    conn.close()


def test_legacy_notes_status(client):
    assert client.get("/api/matchups/legacy-notes").json() == {"count": 0, "notes": {}}
    _seed_legacy_notes(client)
    info = client.get("/api/matchups/legacy-notes").json()
    assert info["count"] == 2
    assert info["notes"]["Darius"] == {"notes": "- care ghost timings", "patch_version": ""}
    # current-schema notes (real my_champion) are not "legacy"
    client.put("/api/matchups/notes/Gwen/Renekton", json={"notes": "new-style"})
    assert client.get("/api/matchups/legacy-notes").json()["count"] == 2


def test_legacy_notes_migrate_moves_rows_and_skips_conflicts(client):
    _seed_legacy_notes(client)
    # Gwen already has her own Darius guide — the legacy Darius row must not clobber it
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "hand-written for Gwen"})
    r = client.post("/api/matchups/legacy-notes/migrate", json={"my_champion": "Gwen"})
    assert r.status_code == 200
    assert r.json() == {"migrated": 1, "skipped": ["Darius"]}
    guide = client.get("/api/matchups/notes?my_champion=Gwen").json()
    assert guide["Teemo"]["notes"] == "ban it"
    assert guide["Darius"]["notes"] == "hand-written for Gwen"  # untouched
    assert client.get("/api/matchups/legacy-notes").json()["count"] == 1  # Darius stays legacy
    # a conflict-free target champion takes the remainder
    r = client.post("/api/matchups/legacy-notes/migrate", json={"my_champion": "Camille"})
    assert r.json() == {"migrated": 1, "skipped": []}
    assert client.get("/api/matchups/legacy-notes").json()["count"] == 0
    assert client.get("/api/matchups/notes?my_champion=Camille").json()["Darius"]["notes"] \
        == "- care ghost timings"
    # validation
    assert client.post("/api/matchups/legacy-notes/migrate", json={}).status_code == 400
    assert client.post("/api/matchups/legacy-notes/migrate",
                       json={"my_champion": "NotAChamp"}).status_code == 400


def test_legacy_notes_delete(client):
    _seed_legacy_notes(client)
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "keep me"})
    assert client.delete("/api/matchups/legacy-notes").json() == {"deleted": 2}
    assert client.get("/api/matchups/legacy-notes").json()["count"] == 0
    # only legacy rows are deleted — real guides survive
    assert client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]["notes"] == "keep me"


def test_patch_version_validation(client):
    ok = {"notes": "x", "patch_version": "16.14"}
    assert client.put("/api/matchups/notes/Gwen/Darius", json=ok).status_code == 200
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"notes": "x", "patch_version": "16.14.1"}).status_code == 200
    assert client.put("/api/matchups/notes/Gwen/Darius",
                      json={"notes": "x", "patch_version": ""}).status_code == 200
    for bad in ("current", "16", "16.14.1.2", "16.x", "a.b"):
        assert client.put("/api/matchups/notes/Gwen/Darius",
                          json={"notes": "x", "patch_version": bad}).status_code == 400
    # import applies the same check
    bad_export = {"data": {
        "app": "coach-potato", "kind": "champ-guide-export", "version": 1,
        "my_champion": "Gwen", "encrypted": False, "general_notes": "",
        "guide": {"Darius": {"notes": "x", "patch_version": "not-a-patch"}}}}
    assert client.post("/api/matchups/notes/import", json=bad_export).status_code == 400


def test_close_block_rejects_empty_block(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    block_id = db.create_block(conn)
    conn.close()
    assert client.post(f"/api/blocks/{block_id}/close").status_code == 409


def test_block_noted_champions_endpoint(client):
    import os
    assert client.get("/api/blocks/noted-champions").json() == []
    conn = db.connect(os.environ["LOL_DB_PATH"])
    m1 = conn.execute(
        """SELECT p.match_id FROM participants p WHERE p.puuid=?
           AND p.champion_name='Garen' LIMIT 1""", (ME,)).fetchone()["match_id"]
    db.add_game_to_block(conn, m1, ME)
    conn.close()
    assert client.get("/api/blocks/noted-champions").json() == []  # note is empty
    conn = db.connect(os.environ["LOL_DB_PATH"])
    entry = conn.execute("SELECT id FROM block_games").fetchone()["id"]
    db.update_block_game(conn, entry, "respect his Q")
    conn.close()
    assert client.get("/api/blocks/noted-champions").json() == ["Darius"]


def test_block_size_setting_endpoint(client):
    assert client.get("/api/settings").json()["block_size"] == 3
    assert _put_settings(client, block_size=5).status_code == 200
    assert client.get("/api/settings").json()["block_size"] == 5
    assert client.get("/api/blocks").json()["block_size"] == 5
    assert _put_settings(client, block_size=25).status_code == 200
    assert client.get("/api/settings").json()["block_size"] == 25
    assert _put_settings(client, block_size=0).status_code == 400
    assert _put_settings(client, block_size="3").status_code == 400


def test_ui_opacity_setting_endpoint(client):
    assert client.get("/api/settings").json()["ui_opacity"] == 100
    assert _put_settings(client, ui_opacity=60).status_code == 200
    assert client.get("/api/settings").json()["ui_opacity"] == 60
    assert _put_settings(client, ui_opacity=19).status_code == 400
    assert _put_settings(client, ui_opacity=101).status_code == 400
    assert _put_settings(client, ui_opacity="60").status_code == 400


def test_accent_color_setting_endpoint(client):
    assert client.get("/api/settings").json()["accent_color"] is None
    assert _put_settings(client, accent_color="#ff8800").status_code == 200
    assert client.get("/api/settings").json()["accent_color"] == "#ff8800"
    assert _put_settings(client, accent_color=None).status_code == 200
    assert client.get("/api/settings").json()["accent_color"] is None
    assert _put_settings(client, accent_color="ff8800").status_code == 400
    assert _put_settings(client, accent_color="#fff").status_code == 400
    assert _put_settings(client, accent_color=123).status_code == 400


def test_background_image_upload_roundtrip(client):
    assert client.get("/api/settings").json()["background_image"] is False
    assert client.get("/api/settings/background/file").status_code == 404

    resp = client.post("/api/settings/background",
                        files={"file": ("bg.png", b"fake png bytes", "image/png")})
    assert resp.status_code == 200
    assert resp.json() == {"background_image": True}
    assert client.get("/api/settings").json()["background_image"] is True

    file_resp = client.get("/api/settings/background/file")
    assert file_resp.status_code == 200
    assert file_resp.content == b"fake png bytes"

    # uploading again replaces the old file (only one lives on disk)
    bg_dir = app_module.get_background_dir()
    assert len(list(bg_dir.iterdir())) == 1
    resp2 = client.post("/api/settings/background",
                         files={"file": ("bg2.jpg", b"other bytes", "image/jpeg")})
    assert resp2.status_code == 200
    assert len(list(bg_dir.iterdir())) == 1
    assert client.get("/api/settings/background/file").content == b"other bytes"

    assert client.delete("/api/settings/background").json() == {"deleted": True}
    assert client.get("/api/settings").json()["background_image"] is False
    assert client.get("/api/settings/background/file").status_code == 404
    assert len(list(bg_dir.iterdir())) == 0


def test_background_image_rejects_bad_extension_and_oversize(client):
    resp = client.post("/api/settings/background",
                        files={"file": ("bg.exe", b"nope", "application/octet-stream")})
    assert resp.status_code == 400
    big = b"x" * (app_module.MAX_BACKGROUND_BYTES + 1)
    resp = client.post("/api/settings/background",
                        files={"file": ("bg.png", big, "image/png")})
    assert resp.status_code == 413


def _garen_and_kled(conn):
    garen = conn.execute(
        "SELECT match_id FROM participants WHERE puuid=? AND champion_name='Garen'"
        " LIMIT 1", (ME,)).fetchone()["match_id"]
    kled = conn.execute(
        "SELECT match_id FROM participants WHERE puuid=? AND champion_name='Kled'",
        (ME,)).fetchone()["match_id"]
    return garen, kled  # ~3.2 years apart in game time


def test_add_game_gap_asks_for_confirmation(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    garen, kled = _garen_and_kled(conn)
    db.add_game_to_block(conn, garen, ME)
    conn.close()
    response = client.post("/api/blocks/games", json={"match_id": kled, "puuid": ME})
    assert response.status_code == 412
    detail = response.json()["detail"]
    assert detail["reason"] == "gap" and detail["block_id"] == 1
    assert detail["gap_hours"] > 3
    # nothing changed yet
    assert client.get("/api/blocks").json()["blocks"][0]["closed"] is False
    # confirmed retry closes block 1 and opens block 2
    response = client.post("/api/blocks/games",
                           json={"match_id": kled, "puuid": ME, "confirm_gap": True})
    assert response.json() == {"block_id": 2}
    blocks = {b["id"]: b for b in client.get("/api/blocks").json()["blocks"]}
    assert blocks[1]["closed"] is True
    # duplicates still 409, never a gap prompt
    assert client.post("/api/blocks/games",
                       json={"match_id": kled, "puuid": ME}).status_code == 409


def test_add_game_gap_silent_when_confirmation_off(client):
    import os
    assert _put_settings(client, block_gap_confirm=False).status_code == 200
    conn = db.connect(os.environ["LOL_DB_PATH"])
    garen, kled = _garen_and_kled(conn)
    db.add_game_to_block(conn, garen, ME)
    conn.close()
    response = client.post("/api/blocks/games", json={"match_id": kled, "puuid": ME})
    assert response.json() == {"block_id": 2}  # auto-closed without asking
    assert client.get("/api/blocks").json()["blocks"][1]["closed"] is True


def test_block_gap_settings_validation(client):
    assert _put_settings(client, block_gap_hours=1.5).status_code == 200
    assert client.get("/api/settings").json()["block_gap_hours"] == 1.5
    assert _put_settings(client, block_gap_hours=-1).status_code == 400
    assert _put_settings(client, block_gap_hours=999).status_code == 400
    assert _put_settings(client, block_gap_confirm="yes").status_code == 400


# ---------- clips ----------

def _make_session(client):
    return client.post("/api/sessions", json={"date": "2026-06-28", "title": "waves"}).json()["id"]


def _make_block_game_entry(client):
    game = client.get("/api/stats/games").json()[0]
    resp = client.post("/api/blocks/games",
                       json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    block_id = resp.json()["block_id"]
    entry = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    return entry["entry_id"], block_id


def test_block_timeline_backfill_endpoint_no_pending(client):
    # nothing in any block → nothing to fetch; must not start a background job
    assert client.post("/api/blocks/backfill-timelines").json() == {"started": False, "pending": 0}
    status = client.get("/api/blocks/timeline-status").json()
    assert status["running"] is False and "done" in status and "total" in status


def test_block_timeline_backfill_counts_pending_without_starting_when_busy(client, monkeypatch):
    # a block game with a metrics row lacking timeline data is "pending"
    entry_id, _ = _make_block_game_entry(client)
    game = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    conn = db.connect(app_module.get_db_path())
    from tests.test_stats import add_metrics
    add_metrics(conn, game["match_id"], puuid=game["puuid"], has_timeline=0)
    conn.close()
    # pretend a crawl is already running → endpoint reports pending but doesn't start
    monkeypatch.setitem(app_module.CRAWL_STATE, "running", True)
    body = client.post("/api/blocks/backfill-timelines").json()
    assert body == {"started": False, "pending": 1}
    monkeypatch.setitem(app_module.CRAWL_STATE, "running", False)


def test_live_game_endpoint(client, monkeypatch):
    from server import riot_client
    _put_settings(client)  # configure so the endpoint can build a client

    def not_in_game(self, puuid):
        raise riot_client.NotFoundError(puuid)
    monkeypatch.setattr(riot_client.RiotClient, "get_active_game", not_in_game)
    assert client.get("/api/live-game").json() == {"found": False}

    def in_game(self, puuid):
        return {"gameQueueConfigId": 420, "participants": [
            {"puuid": puuid, "teamId": 100, "championId": 111},
            {"puuid": "ally", "teamId": 100, "championId": 444},
            {"puuid": "e1", "teamId": 200, "championId": 222},
            {"puuid": "e2", "teamId": 200, "championId": 333}]}
    monkeypatch.setattr(riot_client.RiotClient, "get_active_game", in_game)
    data = client.get("/api/live-game").json()
    assert data["found"] is True
    assert data["my_champion_id"] == 111
    assert sorted(data["enemy_champion_ids"]) == [222, 333]
    assert data["ally_champion_ids"] == [444]


def test_date_format_setting(client):
    assert client.get("/api/settings").json()["date_format"] == "iso"  # default
    for fmt in ("us", "eu", "iso"):
        assert _put_settings(client, date_format=fmt).status_code == 200
        assert client.get("/api/settings").json()["date_format"] == fmt
    assert _put_settings(client, date_format="klingon").status_code == 400


def test_block_indices_gapless_after_delete(client):
    games = client.get("/api/stats/games").json()
    client.post("/api/blocks/games",
                json={"match_id": games[0]["match_id"], "puuid": games[0]["my_puuid"]})
    blocks = client.get("/api/blocks").json()["blocks"]
    assert blocks[0]["global_index"] == 1 and blocks[0]["series_index"] == 1
    client.delete(f"/api/blocks/{blocks[0]['id']}")
    # a new block after deleting the first must reuse #1, not skip to #2
    client.post("/api/blocks/games",
                json={"match_id": games[1]["match_id"], "puuid": games[1]["my_puuid"]})
    blocks = client.get("/api/blocks").json()["blocks"]
    assert blocks[0]["global_index"] == 1
    assert blocks[0]["id"] != 1  # a fresh row id, but the displayed index is still #1


def test_block_series_endpoint_and_setting(client):
    assert client.get("/api/settings").json()["block_series_enabled"] is True
    assert client.get("/api/blocks").json()["series_enabled"] is True
    games = client.get("/api/stats/games").json()
    # a game before starting a series lands in the default series at #1
    client.post("/api/blocks/games",
                json={"match_id": games[0]["match_id"], "puuid": games[0]["my_puuid"]})
    # start a named series; the just-added (non-empty) block closes, the next
    # game opens a new block that is #1 of the new series
    assert client.post("/api/blocks/series", json={"title": "2 Week Challenge"}).status_code == 200
    client.post("/api/blocks/games",
                json={"match_id": games[1]["match_id"], "puuid": games[1]["my_puuid"]})
    blocks = client.get("/api/blocks").json()["blocks"]  # newest first
    assert blocks[0]["series_title"] == "2 Week Challenge"
    assert blocks[0]["series_index"] == 1        # per-series numbering restarts
    assert blocks[0]["global_index"] == 2        # but the global count continues
    # toggling the setting off is reflected
    assert _put_settings(client, block_series_enabled=False).status_code == 200
    assert client.get("/api/blocks").json()["series_enabled"] is False
    assert _put_settings(client, block_series_enabled="nope").status_code == 400


def test_clip_link_roundtrip_for_session(client):
    session_id = _make_session(client)
    assert client.get(f"/api/clips?owner_type=session&owner_id={session_id}").json() == []
    r = client.post("/api/clips", data={
        "owner_type": "session", "owner_id": session_id,
        "label": "wave management @14min", "url": "https://youtu.be/abc123",
    })
    assert r.status_code == 200
    clip = r.json()
    assert clip["kind"] == "link"
    assert clip["play_url"] == "https://youtu.be/abc123"
    assert clip["label"] == "wave management @14min"
    clips = client.get(f"/api/clips?owner_type=session&owner_id={session_id}").json()
    assert len(clips) == 1
    assert client.delete(f"/api/clips/{clip['id']}").status_code == 200
    assert client.get(f"/api/clips?owner_type=session&owner_id={session_id}").json() == []


def test_clip_upload_roundtrip_for_block_game(client):
    entry_id, _ = _make_block_game_entry(client)
    r = client.post("/api/clips",
                    data={"owner_type": "block_game", "owner_id": entry_id, "label": "dive call"},
                    files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")})
    assert r.status_code == 200
    clip = r.json()
    assert clip["kind"] == "upload"
    assert clip["play_url"] == f"/api/clips/{clip['id']}/file"
    file_resp = client.get(clip["play_url"])
    assert file_resp.status_code == 200
    assert file_resp.content == b"fake video bytes"
    assert client.delete(f"/api/clips/{clip['id']}").status_code == 200
    assert client.get(clip["play_url"]).status_code == 404  # file removed from disk too


def test_clip_upload_rejects_oversize_and_bad_extension(client):
    session_id = _make_session(client)
    big = b"x" * (50 * 1024 * 1024 + 1)
    r = client.post("/api/clips", data={"owner_type": "session", "owner_id": session_id},
                    files={"file": ("clip.mp4", big, "video/mp4")})
    assert r.status_code == 413
    r = client.post("/api/clips", data={"owner_type": "session", "owner_id": session_id},
                    files={"file": ("clip.exe", b"nope", "application/octet-stream")})
    assert r.status_code == 400


def test_clip_requires_exactly_one_of_file_or_url(client):
    session_id = _make_session(client)
    assert client.post("/api/clips",
                       data={"owner_type": "session", "owner_id": session_id}).status_code == 400
    assert client.post("/api/clips", data={
        "owner_type": "session", "owner_id": session_id, "url": "https://x.test/a",
    }, files={"file": ("clip.mp4", b"x", "video/mp4")}).status_code == 400


def test_clip_rejects_unknown_owner(client):
    assert client.post("/api/clips", data={
        "owner_type": "session", "owner_id": 999, "url": "https://x.test/a",
    }).status_code == 404
    assert client.post("/api/clips", data={
        "owner_type": "spaceship", "owner_id": 1, "url": "https://x.test/a",
    }).status_code == 400


def test_deleting_session_cleans_up_its_clips(client):
    session_id = _make_session(client)
    r = client.post("/api/clips",
                    data={"owner_type": "session", "owner_id": session_id, "label": "x"},
                    files={"file": ("clip.mp4", b"bytes", "video/mp4")})
    play_url = r.json()["play_url"]
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(play_url).status_code == 404


def test_deleting_block_cleans_up_its_games_clips(client):
    entry_id, block_id = _make_block_game_entry(client)
    r = client.post("/api/clips",
                    data={"owner_type": "block_game", "owner_id": entry_id, "label": "x"},
                    files={"file": ("clip.mp4", b"bytes", "video/mp4")})
    play_url = r.json()["play_url"]
    assert client.delete(f"/api/blocks/{block_id}").status_code == 200
    assert client.get(play_url).status_code == 404


def test_research_entry_crud(client):
    assert client.get("/api/research").json() == []
    r = client.post("/api/research", json={
        "player_name": "Faker", "champion": "Azir", "opp_champion": "Zed",
        "title": "Level 1 pathing", "notes": "interesting recall timing"})
    assert r.status_code == 200
    entry = r.json()
    assert entry["player_name"] == "Faker"
    assert entry["screenshots"] == []
    entry_id = entry["id"]

    listed = client.get("/api/research").json()
    assert len(listed) == 1 and listed[0]["id"] == entry_id

    r = client.patch(f"/api/research/{entry_id}", json={"notes": "updated notes"})
    assert r.status_code == 200
    assert r.json()["notes"] == "updated notes"
    assert r.json()["player_name"] == "Faker"  # unspecified fields untouched

    assert client.post("/api/research", json={"champion": "NotAChamp",
                                               "player_name": "x"}).status_code == 400
    assert client.post("/api/research", json={"player_name": ""}).status_code == 400

    assert client.delete(f"/api/research/{entry_id}").status_code == 200
    assert client.get("/api/research").json() == []
    assert client.delete(f"/api/research/{entry_id}").status_code == 404
    assert client.get(f"/api/research/{entry_id}").status_code == 404


def test_research_entry_screenshots(client):
    entry_id = client.post("/api/research", json={"player_name": "Faker"}).json()["id"]
    r = client.post(f"/api/research/{entry_id}/screenshots", data={"caption": "level 1 setup"},
                    files={"file": ("shot.png", b"fake png bytes", "image/png")})
    assert r.status_code == 200
    screenshots = r.json()
    assert len(screenshots) == 1
    shot = screenshots[0]
    assert shot["caption"] == "level 1 setup"
    file_resp = client.get(shot["file_url"])
    assert file_resp.status_code == 200
    assert file_resp.content == b"fake png bytes"

    big = b"x" * (15 * 1024 * 1024 + 1)
    assert client.post(f"/api/research/{entry_id}/screenshots",
                       files={"file": ("shot.png", big, "image/png")}).status_code == 413
    assert client.post(f"/api/research/{entry_id}/screenshots",
                       files={"file": ("shot.exe", b"x", "application/octet-stream")}
                       ).status_code == 400
    assert client.post("/api/research/999/screenshots",
                       files={"file": ("shot.png", b"x", "image/png")}).status_code == 404

    assert client.delete(f"/api/research/screenshots/{shot['id']}").status_code == 200
    assert client.get(shot["file_url"]).status_code == 404


def test_research_entry_rejects_clip_attachment(client):
    entry_id = client.post("/api/research", json={"player_name": "Faker"}).json()["id"]
    r = client.post("/api/clips", data={
        "owner_type": "research_entry", "owner_id": entry_id, "url": "https://youtu.be/abc"})
    assert r.status_code == 400  # research entries deliberately don't support clips/VODs


def test_deleting_research_entry_cleans_up_screenshots(client):
    entry_id = client.post("/api/research", json={"player_name": "Faker"}).json()["id"]
    shot = client.post(f"/api/research/{entry_id}/screenshots",
                       files={"file": ("shot.png", b"bytes", "image/png")}).json()[0]
    assert client.delete(f"/api/research/{entry_id}").status_code == 200
    assert client.get(shot["file_url"]).status_code == 404


# ---------- export everything ----------

def test_export_all_bundles_content_and_files(client):
    import io
    import zipfile

    session_id = _make_session(client)
    client.put("/api/champions/notes/Gwen", json={"notes": "general Gwen tips"})
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "respect level 2"})
    client.put("/api/champions/item-build/Gwen", json={
        "core": ["Riftmaker"], "situational": []})
    entry_id = client.post("/api/research", json={"player_name": "Faker"}).json()["id"]
    client.post(f"/api/research/{entry_id}/screenshots",
               files={"file": ("shot.png", b"fake png bytes", "image/png")})
    client.post("/api/clips", data={"owner_type": "session", "owner_id": session_id, "label": "x"},
               files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")})

    r = client.get("/api/export-all")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "coach-potato-export-" in r.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "data.json" in names
    assert any(n.startswith("screenshots/") for n in names)
    assert any(n.startswith("clips/") for n in names)

    import json as json_module
    data = json_module.loads(zf.read("data.json"))
    assert data["kind"] == "full-export"
    assert len(data["sessions"]) == 1
    assert data["champion_notes"][0]["notes"] == "general Gwen tips"
    assert data["matchup_notes"][0]["notes"] == "respect level 2"
    assert data["item_builds"][0]["core"] == ["Riftmaker"]
    assert data["research_entries"][0]["player_name"] == "Faker"
    assert len(data["research_screenshots"]) == 1
    assert len(data["clips"]) == 1
    assert "riot_api_key" not in json_module.dumps(data)  # no credentials in the backup


# ---------- import everything ----------

def test_import_all_round_trip_and_conflict_detection(client):
    import io
    import os
    import zipfile

    session_id = _make_session(client)
    client.put("/api/champions/notes/Gwen", json={"notes": "general Gwen tips"})
    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "respect level 2"})
    saved_order = ["Q", "W", "E", "Q", "Q", "R"] + [""] * 12
    client.put("/api/matchups/notes/Gwen/Darius", json={"skill_order": saved_order})
    client.put("/api/champions/item-build/Gwen", json={"core": ["Riftmaker"], "situational": []})
    entry_id = client.post("/api/research", json={"player_name": "Faker"}).json()["id"]
    client.post(f"/api/research/{entry_id}/screenshots",
               files={"file": ("shot.png", b"fake png bytes", "image/png")})
    client.post("/api/clips", data={"owner_type": "session", "owner_id": session_id, "label": "x"},
               files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")})

    export_bytes = client.get("/api/export-all").content

    # importing the same backup back into the same (still-populated) db must
    # detect every conflict and refuse to write anything
    preview = client.post("/api/import-all/preview",
                          files={"file": ("backup.zip", export_bytes, "application/zip")}).json()
    assert preview["counts"]["sessions"] == 1
    assert len(preview["conflicts"]) > 0
    result = client.post("/api/import-all",
                         files={"file": ("backup.zip", export_bytes, "application/zip")})
    assert result.status_code == 409

    # wipe the tables the backup covers to simulate a fresh/empty setup,
    # then the same backup should import cleanly
    conn = db.connect(os.environ["LOL_DB_PATH"])
    for table in ("coaching_sessions", "blocks", "block_games", "matchup_notes",
                  "champion_notes", "champion_item_builds", "research_entries",
                  "research_screenshots", "clips"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

    preview2 = client.post("/api/import-all/preview",
                           files={"file": ("backup.zip", export_bytes, "application/zip")}).json()
    assert preview2["conflicts"] == []

    result2 = client.post("/api/import-all",
                          files={"file": ("backup.zip", export_bytes, "application/zip")})
    assert result2.status_code == 200
    assert result2.json()["imported"]["sessions"] == 1

    assert client.get("/api/champions/notes/Gwen").json()["notes"] == "general Gwen tips"
    restored = client.get("/api/matchups/notes?my_champion=Gwen").json()["Darius"]
    assert restored["notes"] == "respect level 2"
    assert restored["skill_order"] == saved_order  # saved builds survive backups
    assert client.get("/api/champions/item-build/Gwen").json()["core"] == ["Riftmaker"]
    research = client.get("/api/research").json()
    assert research[0]["player_name"] == "Faker"
    entry = client.get(f"/api/research/{research[0]['id']}").json()
    assert len(entry["screenshots"]) == 1
    assert client.get(entry["screenshots"][0]["file_url"]).content == b"fake png bytes"
    clips = client.get(f"/api/clips?owner_type=session&owner_id={session_id}").json()
    assert len(clips) == 1
    assert client.get(clips[0]["play_url"]).content == b"fake video bytes"


def test_export_all_covers_every_matchup_notes_column(client):
    """Column-drift guard: a new matchup_notes column must be carried by the
    backup (add it to export-all + import-all) or this fails. Regression:
    PR #5 predated skill_order and would have silently dropped saved builds."""
    import io
    import os
    import zipfile

    import json as json_module

    client.put("/api/matchups/notes/Gwen/Darius", json={"notes": "x"})
    conn = db.connect(os.environ["LOL_DB_PATH"])
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(matchup_notes)")}
    conn.close()
    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/export-all").content))
    exported = json_module.loads(zf.read("data.json"))["matchup_notes"][0].keys()
    assert columns <= set(exported)


def test_import_all_rejects_bad_files(client):
    import io
    import zipfile

    assert client.post("/api/import-all/preview",
                       files={"file": ("x.zip", b"not a zip", "application/zip")}
                       ).status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.json", '{"kind": "champ-guide-export"}')
    assert client.post("/api/import-all/preview",
                       files={"file": ("x.zip", buf.getvalue(), "application/zip")}
                       ).status_code == 400


def test_tier_lists_crud_and_validation(client):
    assert client.get("/api/tier-lists").json() == []
    # create: unknown champ dropped, case-folded dupe removed, dedup across tiers
    created = client.post("/api/tier-lists", json={"title": "Top", "data": {"tiers": [
        {"label": "S", "color": "#ff0000", "champions": ["Garen", "Bogus", "garen", "Darius"]}]}}).json()
    tid = created["id"]
    assert created["title"] == "Top"
    assert created["data"]["tiers"][0]["champions"] == ["Garen", "Darius"]
    assert [l["id"] for l in client.get("/api/tier-lists").json()] == [tid]
    # update title + data
    up = client.put(f"/api/tier-lists/{tid}", json={"title": "Top lane", "data": {"tiers": [
        {"label": "A", "color": "#00ff00", "champions": ["Teemo"]}]}}).json()
    assert up["title"] == "Top lane"
    assert up["data"]["tiers"][0]["label"] == "A"
    assert up["data"]["tiers"][0]["champions"] == ["Teemo"]
    # bad colour rejected; missing list 404
    assert client.put(f"/api/tier-lists/{tid}",
                      json={"data": {"tiers": [{"color": "red"}]}}).status_code == 400
    assert client.put("/api/tier-lists/9999", json={"title": "x"}).status_code == 404
    # delete
    assert client.delete(f"/api/tier-lists/{tid}").status_code == 200
    assert client.get("/api/tier-lists").json() == []
    assert client.delete(f"/api/tier-lists/{tid}").status_code == 404


def test_champion_tier_lists_save_multiple_and_overwrite_by_title(client):
    assert client.get("/api/champions/Fiora/tier-lists").json() == []  # nothing auto-created
    first = client.post("/api/champions/Fiora/tier-lists", json={
        "title": "vs melee",
        "data": {"tiers": [{"label": "S", "color": "#ff0000",
                            "champions": ["Darius", "bogus"]}]}}).json()
    assert (first["champion"], first["replaced"]) == ("Fiora", False)
    assert first["data"]["tiers"][0]["champions"] == ["Darius"]  # bogus dropped
    # a champion can hold more than one saved list
    second = client.post("/api/champions/Fiora/tier-lists",
                         json={"title": "vs ranged", "data": {"tiers": []}}).json()
    assert second["id"] != first["id"]
    assert [t["title"] for t in client.get("/api/champions/Fiora/tier-lists").json()] \
        == ["vs melee", "vs ranged"]
    # re-saving the same title overwrites that copy rather than piling up
    again = client.post("/api/champions/Fiora/tier-lists", json={
        "title": "VS Melee",
        "data": {"tiers": [{"label": "A", "color": "#00ff00", "champions": ["Garen"]}]}}).json()
    assert (again["id"], again["replaced"]) == (first["id"], True)
    assert again["data"]["tiers"][0]["champions"] == ["Garen"]
    assert len(client.get("/api/champions/Fiora/tier-lists").json()) == 2
    # guide copies stay out of the standalone Tier list tab, and off other champions
    assert client.get("/api/tier-lists").json() == []
    # ...but the compare window sees everything, guide copies carrying their champion
    every = client.get("/api/tier-lists?scope=all").json()
    assert {t["id"] for t in every} == {first["id"], second["id"]}
    assert {t["champion"] for t in every} == {"Fiora"}
    assert client.get("/api/champions/Gwen/tier-lists").json() == []
    assert client.post("/api/champions/NotAChamp/tier-lists", json={}).status_code == 400
    # removing one from the guide is the shared delete endpoint
    assert client.delete(f"/api/tier-lists/{first['id']}").status_code == 200
    assert [t["id"] for t in client.get("/api/champions/Fiora/tier-lists").json()] == [second["id"]]


def test_tier_row_icon_validation(client):
    r = client.post("/api/tier-lists", json={"title": "t", "data": {"tiers": [
        {"label": "S", "image": "Teemo", "champions": []},
        {"label": "A", "image": "NotAChamp", "champions": []}]}}).json()
    tiers = r["data"]["tiers"]
    assert (tiers[0]["image"], tiers[0]["image_kind"]) == ("Teemo", "champion")
    assert (tiers[1]["image"], tiers[1]["image_kind"]) == ("", "champion")  # unknown dropped
    client.delete(f"/api/tier-lists/{r['id']}")


def test_block_game_exposes_detected_strong_weak_side(client):
    import os
    game = client.get("/api/stats/games").json()[0]
    client.post("/api/blocks/games",
                json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    stored = lambda: client.get("/api/blocks").json()["blocks"][0]["games"][0]  # noqa: E731
    # nothing detected yet: the fixture matches have no jungle start recorded
    assert stored()["auto_strongside"] is None
    assert stored()["my_jungle_half"] is None

    conn = db.connect(os.environ["LOL_DB_PATH"])
    # tracked player is TOP on team 100; their jungler started bot (opposite the
    # lane -> strong side), the enemy's started top (same half -> enemy is weak)
    db.set_match_jungle_starts(conn, game["match_id"], {100: "bot", 200: "top"})
    conn.close()
    row = stored()
    assert row["auto_strongside"] is True
    assert row["opp_auto_strongside"] is False
    assert (row["my_jungle_half"], row["opp_jungle_half"]) == ("bot", "top")
    # the manual flag is untouched by detection — it's the override
    assert row["weakside"] is None


def test_block_game_undetermined_jungle_start_reads_as_unknown(client):
    import os
    game = client.get("/api/stats/games").json()[0]
    client.post("/api/blocks/games",
                json={"match_id": game["match_id"], "puuid": game["my_puuid"]})
    conn = db.connect(os.environ["LOL_DB_PATH"])
    db.set_match_jungle_starts(conn, game["match_id"], {100: None, 200: None})
    conn.close()
    row = client.get("/api/blocks").json()["blocks"][0]["games"][0]
    # '' in the db must surface as None, not as a falsy half that grades wrongly
    assert row["my_jungle_half"] is None
    assert row["auto_strongside"] is None


def test_blocks_expose_current_series_before_any_game(client):
    # the bug this fixes: a series started before its first game was invisible,
    # because the payload only described series through the blocks in them
    body = client.get("/api/blocks").json()
    assert body["blocks"] == []
    assert body["current_series_id"] is not None
    assert len(body["series"]) == 1
    assert body["series"][0]["id"] == body["current_series_id"]
    assert body["series"][0]["goals"] == ""

    new_id = client.post("/api/blocks/series", json={"title": "Two-week challenge"}).json()["series_id"]
    body = client.get("/api/blocks").json()
    assert body["current_series_id"] == new_id  # visible with zero blocks in it
    assert body["series"][0]["title"] == "Two-week challenge"


def test_update_block_series_title_and_goals_independently(client):
    sid = client.get("/api/blocks").json()["current_series_id"]
    assert client.patch(f"/api/blocks/series/{sid}",
                        json={"goals": "- 70 CS by 10m\n- no solo deaths"}).status_code == 200
    assert client.patch(f"/api/blocks/series/{sid}", json={"title": "August"}).status_code == 200
    series = client.get("/api/blocks").json()["series"][0]
    assert series["title"] == "August"           # goals edit didn't clobber it
    assert series["goals"].startswith("- 70 CS")  # nor the reverse
    assert client.patch(f"/api/blocks/series/{sid}", json={}).status_code == 400
    assert client.patch("/api/blocks/series/9999", json={"title": "x"}).status_code == 404


def test_block_series_goals_survive_a_new_series(client):
    first = client.get("/api/blocks").json()["current_series_id"]
    client.patch(f"/api/blocks/series/{first}", json={"goals": "first goals"})
    client.post("/api/blocks/series", json={"title": "second"})
    series = {s["id"]: s for s in client.get("/api/blocks").json()["series"]}
    assert len(series) == 2
    assert series[first]["goals"] == "first goals"  # older series keeps its goals


def test_series_closing_notes_round_trip_and_independence(client):
    sid = client.get("/api/blocks").json()["current_series_id"]
    assert client.get("/api/blocks").json()["series"][0]["closing_notes"] == ""
    assert client.patch(f"/api/blocks/series/{sid}", json={
        "closing_notes": "## How it went\n- hit 70 CS in 6/10 games"}).status_code == 200
    # the retrospective must not disturb the goals it's judged against
    client.patch(f"/api/blocks/series/{sid}", json={"goals": "- 70 CS by 10m"})
    client.patch(f"/api/blocks/series/{sid}", json={"title": "August"})
    series = client.get("/api/blocks").json()["series"][0]
    assert series["closing_notes"].startswith("## How it went")
    assert series["goals"] == "- 70 CS by 10m"
    assert series["title"] == "August"


def test_series_closing_notes_kept_when_a_new_series_starts(client):
    first = client.get("/api/blocks").json()["current_series_id"]
    client.patch(f"/api/blocks/series/{first}", json={"closing_notes": "wrapped up"})
    client.post("/api/blocks/series", json={"title": "next"})
    series = {s["id"]: s for s in client.get("/api/blocks").json()["series"]}
    assert series[first]["closing_notes"] == "wrapped up"
    assert series[max(series)]["closing_notes"] == ""  # the new one starts blank


def test_comparison_scopes_measure_you_and_others_the_same_way(client):
    # matchup scope: your Gwen-vs-Darius games (2 in the fixture) plus the
    # champion baseline to read them against
    body = client.get("/api/comparison", params={
        "scope": "matchup", "my_champion": "Garen", "opp_champion": "Darius"}).json()
    assert body["scope"] == "matchup"
    assert body["you"]["scoped"]["games"] == 2
    assert body["you"]["overall"]["games"] == 2   # Garen vs everyone
    assert len(body["you"]["recent"]) == 2
    assert body["players"] == []                  # comparison disabled by default

    # champion scope: no opponent filter, and no baseline to compare against
    body = client.get("/api/comparison", params={
        "scope": "champion", "my_champion": "Garen"}).json()
    assert body["you"]["scoped"]["games"] == 2
    assert body["you"]["overall"] is None
    assert body["opp_champion"] == ""

    # overall scope: every tracked game regardless of champion
    body = client.get("/api/comparison", params={"scope": "overall"}).json()
    assert body["you"]["scoped"]["games"] == 3    # Garen x2 + Kled
    assert body["my_champion"] == ""


def test_comparison_rejects_unknown_scope_and_champion(client):
    assert client.get("/api/comparison", params={"scope": "nonsense"}).status_code == 400
    assert client.get("/api/comparison", params={
        "scope": "champion", "my_champion": "NotAChampion"}).status_code == 400


def test_comparison_includes_enabled_players_alongside_you(client):
    import os
    conn = db.connect(os.environ["LOL_DB_PATH"])
    # set the flag directly: the settings endpoint refuses to save without an
    # API key, which this fixture db has no reason to carry
    db.set_settings(conn, {"enable_player_comparison": "1"})
    db.add_comparison_player(conn, "rival-1", "Rival", "EUW", "euw1")
    conn.close()
    body = client.get("/api/comparison", params={"scope": "overall"}).json()
    assert [p["game_name"] for p in body["players"]] == ["Rival"]
    # a player with no crawled games still gets a zeroed row, not a missing one
    assert body["players"][0]["scoped"]["games"] == 0
    assert body["you"]["scoped"]["games"] == 3


def test_session_coach_round_trip_and_suggestions(client):
    assert client.post("/api/sessions", json={
        "date": "2026-08-01", "title": "waves", "coach": " LS "}).status_code == 200
    session = client.get("/api/sessions").json()[0]
    assert session["coach"] == "LS"                      # trimmed
    assert client.get("/api/coaches").json()["coaches"] == ["LS"]

    # a coach can be backfilled onto an older session, and joins the list
    client.post("/api/sessions", json={"date": "2026-08-08"})
    older = [s for s in client.get("/api/sessions").json() if s["session_date"] == "2026-08-08"][0]
    assert older["coach"] == ""
    assert client.patch(f"/api/sessions/{older['id']}",
                        json={"coach": "Coach Curtis"}).status_code == 200
    assert client.get("/api/coaches").json()["coaches"] == ["Coach Curtis", "LS"]
    # and editing the coach alone leaves title/notes alone
    assert [s for s in client.get("/api/sessions").json()
            if s["session_date"] == "2026-08-01"][0]["title"] == "waves"


def test_forgetting_a_coach_only_stops_the_suggestion(client):
    client.post("/api/sessions", json={"date": "2026-08-01", "coach": "LS"})
    assert client.delete("/api/coaches/LS").status_code == 200
    assert client.get("/api/coaches").json()["coaches"] == []
    # the session that recorded them is untouched — this list is autocomplete,
    # not the record of who coached what
    assert client.get("/api/sessions").json()[0]["coach"] == "LS"
    assert client.delete("/api/coaches/LS").status_code == 404


def test_session_export_names_the_coach(client):
    client.post("/api/sessions", json={
        "date": "2026-08-01", "title": "waves", "coach": "LS", "notes": "- freeze"})
    text = client.get("/api/sessions/export.md").text
    assert "Coach: LS" in text and "- freeze" in text


def test_progress_default_tab_setting(client):
    base = client.get("/api/settings").json()
    assert base["progress_default_tab"] == "progress"
    # saving settings at all needs a key + account present in the payload
    base = {**base, "riot_api_key": "RGAPI-x", "accounts": ["Me#EUW"]}
    assert client.put("/api/settings", json={**base, "progress_default_tab": "sessions"}).status_code == 200
    assert client.get("/api/settings").json()["progress_default_tab"] == "sessions"
    assert client.put("/api/settings", json={**base, "progress_default_tab": "nope"}).status_code == 400
