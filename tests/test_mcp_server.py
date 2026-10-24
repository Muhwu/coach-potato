import json

import pytest
from fastapi.testclient import TestClient

from server import db, mcp_server
from server.app import app

from tests.test_stats import ME, add_match  # reuse fixture builder


def rpc(method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def call(conn, name, arguments=None):
    """tools/call through the protocol layer, returning the parsed result."""
    response = mcp_server.handle(conn, rpc("tools/call",
                                          {"name": name, "arguments": arguments or {}}))
    result = response["result"]
    text = result["content"][0]["text"]
    return result, (text if result["isError"] else json.loads(text))


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.sqlite")
    db.upsert_player(connection, ME, "PlayerOne", "EUW", is_tracked=True)
    add_match(connection, my_champ="Gwen", opp_champ="Darius", win=True,
              when=1_700_000_000_000)
    add_match(connection, my_champ="Gwen", opp_champ="Darius", win=False,
              when=1_700_000_100_000)
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app with the MCP endpoint switched on and a known token."""
    from server import config
    db_path = tmp_path / "app.sqlite"
    monkeypatch.setenv("LOL_DB_PATH", str(db_path))
    monkeypatch.setattr(config, "ENV_FALLBACK_ROOT", tmp_path)  # ignore repo .env
    connection = db.connect(db_path)
    db.upsert_player(connection, ME, "PlayerOne", "EUW", is_tracked=True)
    add_match(connection, my_champ="Gwen", opp_champ="Darius", win=True,
              when=1_700_000_000_000)
    db.set_settings(connection, {"mcp_enabled": "1", "mcp_token": "test-token"})
    connection.close()
    with TestClient(app) as test_client:
        yield test_client


# ----------------------------------------------------------------- protocol

def test_initialize_echoes_a_protocol_version_it_knows(conn):
    result = mcp_server.handle(conn, rpc("initialize",
                                         {"protocolVersion": "2025-06-18"}))["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "coach-potato"
    assert "coaching database" in result["instructions"]


def test_initialize_falls_back_to_our_latest_for_an_unknown_version(conn):
    result = mcp_server.handle(conn, rpc("initialize",
                                         {"protocolVersion": "1999-01-01"}))["result"]
    assert result["protocolVersion"] == mcp_server.LATEST_PROTOCOL


def test_notifications_get_no_response_at_all(conn):
    assert mcp_server.handle(conn, {"jsonrpc": "2.0",
                                    "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_jsonrpc_error(conn):
    response = mcp_server.handle(conn, rpc("resources/list"))
    assert response["error"]["code"] == -32601


def test_ping(conn):
    assert mcp_server.handle(conn, rpc("ping"))["result"] == {}


def test_tool_list_is_well_formed_and_unique(conn):
    tools = mcp_server.handle(conn, rpc("tools/list"))["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for entry in tools:
        assert entry["description"].strip()
        assert entry["inputSchema"]["type"] == "object"
        assert "handler" not in entry  # python internals never leave the server
    # the write tools are the ones flagged as not read-only
    writes = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
    assert writes == {"add_coaching_session", "update_coaching_session",
                      "update_matchup_notes", "set_game_reflection",
                      "set_block_learnings"}


def test_unknown_tool_is_a_tool_error_not_a_protocol_error(conn):
    result, text = call(conn, "no_such_tool")
    assert result["isError"] is True
    assert "unknown tool" in text


def test_every_read_tool_runs_against_a_real_db(conn):
    """A smoke test over the whole read surface: each tool is callable with no
    arguments and comes back as JSON, not an error."""
    skip = {"get_playbook", "search_notes"}  # these two have required arguments
    for entry in mcp_server.TOOLS:
        if entry["name"] in skip or not entry["annotations"]["readOnlyHint"]:
            continue
        result, _ = call(conn, entry["name"])
        assert result["isError"] is False, f"{entry['name']}: {result}"


# -------------------------------------------------------------- read tools

def test_get_overview_and_matchups(conn):
    _, overview = call(conn, "get_overview")
    assert overview["games"] == 2
    assert [c["champion"] for c in overview["by_champion"]] == ["Gwen"]
    _, matchups = call(conn, "get_matchups", {"champion": "Gwen"})
    assert [row["opp_champion"] for row in matchups] == ["Darius"]


def test_get_recent_games_carries_the_reflection(conn):
    _, games = call(conn, "get_recent_games", {"limit": 1})
    assert len(games) == 1
    assert games[0]["reflection"] == {"tags": [], "note": ""}
    db.set_reflection(conn, games[0]["match_id"], ME, tags=["bad TP"], note="greedy")
    _, games = call(conn, "get_recent_games", {"limit": 1})
    assert games[0]["reflection"] == {"tags": ["bad TP"], "note": "greedy"}


def test_range_and_role_arguments_are_validated(conn):
    result, text = call(conn, "get_overview", {"range": "last tuesday"})
    assert result["isError"] and "range must be one of" in text
    result, text = call(conn, "get_overview", {"role": "SUPPORT"})
    assert result["isError"] and "role must be one of" in text
    result, text = call(conn, "get_recent_games", {"limit": 500})
    assert result["isError"] and "limit must be <= 100" in text


def test_trends_ships_the_metric_meta(conn):
    _, trends = call(conn, "get_trends", {"bucket": "month"})
    assert trends["meta"]["cs_at_10"]["label"] == "CS @ 10 min"
    assert trends["meta"]["cs_at_10"]["direction"] == 1


def test_coaching_progress_says_so_when_there_are_no_sessions(conn):
    _, progress = call(conn, "get_coaching_progress")
    assert progress["segments"] == []
    db.add_session(conn, "2026-01-01", title="first", notes="work on CS")
    _, progress = call(conn, "get_coaching_progress")
    assert progress["segments"]


def test_get_playbook_gathers_notes_build_and_matchups(conn):
    db.set_champion_note(conn, "Gwen", "general plan")
    db.set_item_build(conn, "Gwen", ["Nashor's Tooth"], [])
    db.set_matchup_note(conn, "Gwen", "Darius", notes="respect E", patch_version="16.14")
    _, playbook = call(conn, "get_playbook", {"champion": "Gwen"})
    assert playbook["general_notes"] == "general plan"
    assert playbook["item_build"]["core"] == ["Nashor's Tooth"]
    assert playbook["matchups"]["Darius"]["patch_version"] == "16.14"
    # one opponent: still answers for a matchup with nothing written yet
    _, playbook = call(conn, "get_playbook",
                       {"champion": "Gwen", "opp_champion": "Teemo"})
    assert playbook["matchups"] == {"Teemo": {"notes": "", "runes": [],
                                              "patch_version": "", "skill_order": []}}


def test_playbook_rejects_a_champion_that_is_not_a_ddragon_id(conn):
    result, text = call(conn, "get_playbook", {"champion": "Wukong"})
    assert result["isError"] and "MonkeyKing" in text


def test_search_notes_spans_tables_and_reports_where(conn):
    db.add_session(conn, "2026-02-01", title="vision", notes="ward the river more")
    db.set_matchup_note(conn, "Gwen", "Darius", notes="river control matters")
    _, found = call(conn, "search_notes", {"query": "river"})
    assert {hit["source"] for hit in found["hits"]} == {"coaching_sessions.notes",
                                                       "matchup_notes.notes"}
    assert found["truncated"] is False


def test_search_notes_treats_like_wildcards_as_literal_text(conn):
    db.add_session(conn, "2026-03-01", title="farm", notes="hit 90% of waves")
    _, found = call(conn, "search_notes", {"query": "90%"})
    assert len(found["hits"]) == 1
    # '_' is a LIKE wildcard: escaped, so this must NOT match "90% of waves"
    _, found = call(conn, "search_notes", {"query": "9_%"})
    assert found["hits"] == []


# ------------------------------------------------------------- write tools

def test_add_coaching_session_writes_and_refuses_a_duplicate_date(conn):
    _, added = call(conn, "add_coaching_session",
                    {"session_date": "2026-04-01", "title": "wave control",
                     "coach": "Ada", "notes": "slow push"})
    rows = [dict(r) for r in db.list_sessions(conn)]
    assert [(r["session_date"], r["title"], r["coach"]) for r in rows] == [
        ("2026-04-01", "wave control", "Ada")]
    assert added["id"] == rows[0]["id"]
    result, text = call(conn, "add_coaching_session", {"session_date": "2026-04-01"})
    assert result["isError"] and "already exists" in text


def test_add_coaching_session_validates_the_date(conn):
    result, text = call(conn, "add_coaching_session", {"session_date": "01/04/2026"})
    assert result["isError"] and "YYYY-MM-DD" in text


def test_update_coaching_session_only_touches_what_it_is_given(conn):
    session_id = db.add_session(conn, "2026-05-01", title="keep me", notes="old")
    call(conn, "update_coaching_session", {"session_id": session_id, "notes": "new"})
    row = dict(db.list_sessions(conn)[0])
    assert (row["title"], row["notes"]) == ("keep me", "new")
    result, text = call(conn, "update_coaching_session", {"session_id": 9999,
                                                          "notes": "x"})
    assert result["isError"] and "no session with id 9999" in text


def test_update_matchup_notes_is_a_partial_write(conn):
    call(conn, "update_matchup_notes",
         {"champion": "Gwen", "opp_champion": "Darius", "notes": "respect E"})
    call(conn, "update_matchup_notes",
         {"champion": "Gwen", "opp_champion": "Darius", "patch_version": "16.14"})
    stored = db.get_matchup_notes(conn, "Gwen")["Darius"]
    assert (stored["notes"], stored["patch_version"]) == ("respect E", "16.14")


def test_update_matchup_notes_validates_the_patch(conn):
    result, text = call(conn, "update_matchup_notes",
                        {"champion": "Gwen", "opp_champion": "Darius",
                         "patch_version": "season 16"})
    assert result["isError"] and "16.14" in text


def test_set_game_reflection_needs_a_game_that_exists(conn):
    _, games = call(conn, "get_recent_games", {"limit": 1})
    match_id = games[0]["match_id"]
    _, written = call(conn, "set_game_reflection",
                      {"match_id": match_id, "puuid": ME, "tags": ["int death", " "]})
    assert written["reflection"]["tags"] == ["int death"]  # blanks dropped
    # a note-only follow-up keeps the tags (partial write)
    _, written = call(conn, "set_game_reflection",
                      {"match_id": match_id, "puuid": ME, "note": "flashed early"})
    assert written["reflection"] == {"tags": ["int death"], "note": "flashed early"}
    result, text = call(conn, "set_game_reflection",
                        {"match_id": "EUW1_nope", "puuid": ME, "note": "x"})
    assert result["isError"] and "no stored game" in text


def test_set_game_reflection_rejects_non_string_tags(conn):
    _, games = call(conn, "get_recent_games", {"limit": 1})
    result, text = call(conn, "set_game_reflection",
                        {"match_id": games[0]["match_id"], "puuid": ME, "tags": [3]})
    assert result["isError"] and "list of strings" in text


def test_set_block_learnings(conn):
    _, games = call(conn, "get_recent_games", {"limit": 1})
    db.add_game_to_block(conn, games[0]["match_id"], ME)
    block_id = db.list_blocks(conn)[0]["id"]
    call(conn, "set_block_learnings", {"block_id": block_id,
                                       "learnings": "stop greeding plates"})
    _, learnings = call(conn, "get_learnings")
    blocks = [b for series in learnings for b in series["blocks"]]
    assert [b["learnings"] for b in blocks] == ["stop greeding plates"]
    result, text = call(conn, "set_block_learnings", {"block_id": 9999,
                                                      "learnings": "x"})
    assert result["isError"] and "no block with id 9999" in text


def test_tools_report_when_there_are_no_tracked_accounts(tmp_path):
    empty = db.connect(tmp_path / "empty.sqlite")
    try:
        result, text = call(empty, "get_overview")
        assert result["isError"] and "no tracked accounts" in text
    finally:
        empty.close()


# ------------------------------------------------------------------- token

def test_token_is_minted_once_and_rotatable(conn):
    assert mcp_server.get_token(conn) == ""
    first = mcp_server.ensure_token(conn)
    assert first and mcp_server.ensure_token(conn) == first
    assert mcp_server.token_matches(conn, first)
    second = mcp_server.rotate_token(conn)
    assert second != first
    assert not mcp_server.token_matches(conn, first)


def test_an_unset_token_matches_nothing(conn):
    assert not mcp_server.token_matches(conn, "")
    assert not mcp_server.token_matches(conn, "guess")


def test_disabled_by_default(conn):
    assert mcp_server.is_enabled(conn) is False


# ------------------------------------------------------------- the endpoint

def test_endpoint_rejects_a_request_with_no_token(client):
    response = client.post("/mcp", json=rpc("tools/list"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_endpoint_rejects_a_wrong_token(client):
    response = client.post("/mcp", json=rpc("tools/list"),
                           headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_bearer_header_path_token_and_api_key_header_all_work(client):
    body = rpc("tools/list")
    for kwargs in ({"headers": {"Authorization": "Bearer test-token"}},
                   {"headers": {"Authorization": "test-token"}},  # bare, no scheme
                   {"headers": {"X-Api-Key": "test-token"}}):
        response = client.post("/mcp", json=body, **kwargs)
        assert response.status_code == 200, kwargs
        assert response.json()["result"]["tools"]
    # the capability URL, for clients that can't send a header
    response = client.post("/mcp/test-token", json=body)
    assert response.status_code == 200
    assert response.json()["result"]["tools"]


def test_endpoint_is_invisible_while_the_feature_is_off(client, tmp_path):
    conn = db.connect(tmp_path / "app.sqlite")
    db.set_settings(conn, {"mcp_enabled": "0"})
    conn.close()
    response = client.post("/mcp/test-token", json=rpc("tools/list"))
    assert response.status_code == 404


def test_a_notification_gets_an_empty_202(client):
    response = client.post("/mcp/test-token",
                           json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202
    assert response.content == b""


def test_sse_framing_when_the_client_only_accepts_event_stream(client):
    response = client.post("/mcp/test-token", json=rpc("ping"),
                           headers={"Accept": "text/event-stream"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("event: message\ndata: {")
    payload = json.loads(response.text.split("data: ", 1)[1].strip())
    assert payload["result"] == {}


def test_plain_json_when_the_client_accepts_both(client):
    response = client.post("/mcp/test-token", json=rpc("ping"),
                           headers={"Accept": "application/json, text/event-stream"})
    assert response.headers["content-type"].startswith("application/json")


def test_get_is_not_supported(client):
    assert client.get("/mcp").status_code == 405
    assert client.get("/mcp/test-token").status_code == 405


def test_tool_call_over_http(client):
    response = client.post("/mcp/test-token", json=rpc(
        "tools/call", {"name": "get_overview", "arguments": {}}))
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["games"] == 1


def test_hide_my_rank_is_honoured_through_the_mcp_door_too(client, tmp_path):
    """The response middleware only covers /api — a second door onto the same
    data must not become the exception that leaks LP."""
    conn = db.connect(tmp_path / "app.sqlite")
    db.set_settings(conn, {"hide_my_rank": "1"})
    # a tracked player's own rank lives on `players`, written by the crawler
    # with raw SQL (crawler.refresh_tracked_ranks) — there is no db.py setter
    with conn:
        conn.execute("UPDATE players SET solo_tier='GOLD', solo_division='II', "
                     "solo_lp=42 WHERE puuid=?", (ME,))
    db.add_session(conn, "2026-06-01", title="ranked check")  # snapshots start_ranks
    conn.close()
    response = client.post("/mcp/test-token", json=rpc(
        "tools/call", {"name": "get_coaching_progress", "arguments": {}}))
    text = response.json()["result"]["content"][0]["text"]
    assert "GOLD" not in text


def test_the_tools_listing_endpoint_mirrors_the_registry(client):
    listing = client.get("/api/mcp/tools").json()["tools"]
    assert {t["name"] for t in listing} == {t["name"] for t in mcp_server.TOOLS}
    assert sum(1 for t in listing if t["writes"]) == 5


def test_rotating_the_token_through_the_api_breaks_the_old_one(client):
    fresh = client.post("/api/mcp/token").json()["mcp_token"]
    assert fresh != "test-token"
    assert client.post("/mcp/test-token", json=rpc("ping")).status_code == 401
    assert client.post(f"/mcp/{fresh}", json=rpc("ping")).status_code == 200


def test_settings_expose_the_switch_and_the_token(client):
    settings = client.get("/api/settings").json()
    assert settings["mcp_enabled"] is True
    assert settings["mcp_token"] == "test-token"
