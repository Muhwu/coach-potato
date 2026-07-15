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


def test_matchup_notes_endpoints(client):
    assert client.get("/api/matchups/notes").json() == {}
    r = client.put("/api/matchups/notes/Darius", json={
        "notes": "- respect level 2", "primary_keystone": "Conqueror",
        "secondary_tree": "Resolve", "patch_version": "14.14"})
    assert r.status_code == 200
    assert client.get("/api/matchups/notes").json() == {"Darius": {
        "notes": "- respect level 2", "primary_keystone": "Conqueror",
        "secondary_tree": "Resolve", "patch_version": "14.14"}}
    client.put("/api/matchups/notes/Darius", json={
        "notes": "", "primary_keystone": "", "secondary_tree": "", "patch_version": ""})
    assert client.get("/api/matchups/notes").json() == {}  # all-blank deletes
    assert client.put("/api/matchups/notes/NotAChamp",
                      json={"notes": "x"}).status_code == 400
    assert client.put("/api/matchups/notes/Darius", json={}).status_code == 400
    assert client.put("/api/matchups/notes/Darius",
                      json={"primary_keystone": "Not A Rune"}).status_code == 400
    assert client.put("/api/matchups/notes/Darius",
                      json={"secondary_tree": "Not A Tree"}).status_code == 400


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
    assert client.put("/api/matchups/notes/FiddleSticks",
                      json={"notes": "ban worthy"}).status_code == 200
    assert client.get("/api/matchups/notes").json()["FiddleSticks"]["notes"] == "ban worthy"


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
    assert _put_settings(client, block_size=11).status_code == 400
    assert _put_settings(client, block_size=0).status_code == 400
    assert _put_settings(client, block_size="3").status_code == 400


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
