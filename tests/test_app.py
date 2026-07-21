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
    assert r.status_code == 200 and r.json()["enable_player_comparison"] is True
    assert client.get("/api/comparison-players").json() == {
        "players": [], "max": db.MAX_COMPARISON_PLAYERS}
    # comparison off -> empty player list even if some exist
    client.put("/api/settings", json={**base, "enable_player_comparison": False})
    assert client.get("/api/matchups/comparison",
                      params={"my_champion": "Gwen", "opp_champion": "Darius"}).json() == {"players": []}
    # PATCH validates the enabled flag
    assert client.patch("/api/comparison-players/xyz", json={"enabled": "no"}).status_code == 400


def test_champion_general_notes_endpoints(client):
    assert client.get("/api/champions/notes/Gwen").json() == {"notes": ""}
    r = client.put("/api/champions/notes/Gwen", json={"notes": "- always take Conqueror"})
    assert r.status_code == 200
    assert client.get("/api/champions/notes/Gwen").json() == {"notes": "- always take Conqueror"}
    client.put("/api/champions/notes/Gwen", json={"notes": ""})  # blank deletes
    assert client.get("/api/champions/notes/Gwen").json() == {"notes": ""}
    assert client.put("/api/champions/notes/NotAChamp", json={"notes": "x"}).status_code == 400
    assert client.put("/api/champions/notes/Gwen", json={}).status_code == 400


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
