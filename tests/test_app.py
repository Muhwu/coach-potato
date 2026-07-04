import pytest
from fastapi.testclient import TestClient

from server import db
from server import app as app_module
from server.app import app, parse_time_range

from tests.test_stats import ME, add_match  # reuse fixture builder


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "t.sqlite"
    monkeypatch.setenv("LOL_DB_PATH", str(db_path))
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


def test_index_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
