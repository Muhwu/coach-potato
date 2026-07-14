import pytest

from server import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


def make_participant(puuid="p1", **overrides):
    row = {
        "puuid": puuid,
        "riot_id_name": "Player" + puuid,
        "champion_name": "Garen",
        "team_id": 100,
        "team_position": "TOP",
        "win": 1,
        "kills": 5,
        "deaths": 2,
        "assists": 7,
        "cs": 200,
        "gold_earned": 12000,
        "damage_to_champions": 18000,
    }
    row.update(overrides)
    return row


def make_match(match_id="EUW1_1", **overrides):
    row = {
        "match_id": match_id,
        "queue_id": 420,
        "game_creation_ms": 1_700_000_000_000,
        "game_duration_s": 1800,
        "game_version": "14.1.1",
    }
    row.update(overrides)
    return row


def test_connect_creates_schema_and_parent_dir(tmp_path):
    c = db.connect(tmp_path / "sub" / "x.sqlite")
    tables = {
        r["name"]
        for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"players", "matches", "participants", "player_ranks", "crawl_state"} <= tables
    c.close()


def test_insert_match_stores_match_and_participants(conn):
    inserted = db.insert_match(conn, make_match(), [make_participant("a"), make_participant("b")])
    assert inserted is True
    assert db.has_match(conn, "EUW1_1")
    n = conn.execute("SELECT COUNT(*) AS n FROM participants").fetchone()["n"]
    assert n == 2


def test_insert_match_is_idempotent(conn):
    db.insert_match(conn, make_match(), [make_participant("a")])
    inserted = db.insert_match(conn, make_match(), [make_participant("a")])
    assert inserted is False
    n = conn.execute("SELECT COUNT(*) AS n FROM participants").fetchone()["n"]
    assert n == 1


def test_upsert_player_updates_name_keeps_tracked(conn):
    db.upsert_player(conn, "pu1", "Old", "EUW", is_tracked=True)
    db.upsert_player(conn, "pu1", "New", "EUW", is_tracked=False)
    row = conn.execute("SELECT * FROM players WHERE puuid='pu1'").fetchone()
    assert row["game_name"] == "New"
    assert row["is_tracked"] == 1  # once tracked, stays tracked


def test_player_rank_round_trip(conn):
    assert db.get_player_rank(conn, "pu1") is None
    db.set_player_rank(conn, "pu1", "GOLD", "II", 54, fetched_at_ms=1000)
    row = db.get_player_rank(conn, "pu1")
    assert (row["solo_tier"], row["solo_division"], row["solo_lp"]) == ("GOLD", "II", 54)
    db.set_player_rank(conn, "pu1", "PLATINUM", "IV", 1, fetched_at_ms=2000)
    assert db.get_player_rank(conn, "pu1")["solo_tier"] == "PLATINUM"


def test_unranked_player_rank_stored_as_null_tier(conn):
    db.set_player_rank(conn, "pu1", None, None, None, fetched_at_ms=1000)
    row = db.get_player_rank(conn, "pu1")
    assert row["solo_tier"] is None
    assert row["fetched_at_ms"] == 1000


def test_add_and_list_sessions_sorted_by_date(conn):
    db.add_session(conn, "2026-07-05", "split pushing")
    db.add_session(conn, "2026-06-28", "wave management", notes="# Focus\n- freeze near tower")
    sessions = db.list_sessions(conn)
    assert [s["session_date"] for s in sessions] == ["2026-06-28", "2026-07-05"]
    assert sessions[0]["title"] == "wave management"
    assert sessions[0]["notes"].startswith("# Focus")


def test_add_session_title_and_notes_default_empty(conn):
    session_id = db.add_session(conn, "2026-06-28")
    assert isinstance(session_id, int)
    row = db.list_sessions(conn)[0]
    assert row["title"] == ""
    assert row["notes"] == ""


def test_update_session_partial_updates(conn):
    session_id = db.add_session(conn, "2026-06-28", "old title", notes="old notes")
    assert db.update_session(conn, session_id, title="new title") is True
    row = db.list_sessions(conn)[0]
    assert row["title"] == "new title"
    assert row["notes"] == "old notes"
    assert db.update_session(conn, session_id, notes="new notes") is True
    row = db.list_sessions(conn)[0]
    assert row["title"] == "new title"
    assert row["notes"] == "new notes"


def test_update_session_missing_id_returns_false(conn):
    assert db.update_session(conn, 999, title="x") is False


def test_legacy_note_column_migrates_to_title_and_notes(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE coaching_sessions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_date TEXT NOT NULL UNIQUE,
               note TEXT NOT NULL DEFAULT '',
               created_at_ms INTEGER)"""
    )
    legacy.execute(
        "INSERT INTO coaching_sessions (session_date, note, created_at_ms) VALUES (?,?,?)",
        ("2026-06-28", "first coaching session", 123),
    )
    legacy.commit()
    legacy.close()
    conn = db.connect(path)
    row = db.list_sessions(conn)[0]
    assert row["title"] == "first coaching session"
    assert row["notes"] == ""
    assert row["session_date"] == "2026-06-28"
    conn.close()


def test_add_session_duplicate_date_raises(conn):
    import sqlite3
    db.add_session(conn, "2026-06-28")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_session(conn, "2026-06-28")


def test_delete_session(conn):
    session_id = db.add_session(conn, "2026-06-28")
    assert db.delete_session(conn, session_id) is True
    assert db.list_sessions(conn) == []
    assert db.delete_session(conn, session_id) is False


def test_participant_metrics_round_trip(conn):
    from server.metrics import metric_keys
    values = {k: None for k in metric_keys()}
    values.update({"has_challenges": 1, "cs_at_10": 87, "time_dead": 259})
    db.insert_participant_metrics(conn, "EUW1_1", "p1", values)
    row = conn.execute(
        "SELECT * FROM participant_metrics WHERE match_id='EUW1_1' AND puuid='p1'"
    ).fetchone()
    assert row["cs_at_10"] == 87
    assert row["time_dead"] == 259
    assert row["max_cs_lead"] is None
    # replace on re-insert
    values["cs_at_10"] = 90
    db.insert_participant_metrics(conn, "EUW1_1", "p1", values)
    row = conn.execute("SELECT cs_at_10 FROM participant_metrics").fetchone()
    assert row["cs_at_10"] == 90


def test_participant_metrics_table_added_to_existing_db(tmp_path):
    # connect twice: second connect must not fail and table must exist
    c1 = db.connect(tmp_path / "x.sqlite")
    c1.close()
    c2 = db.connect(tmp_path / "x.sqlite")
    assert c2.execute(
        "SELECT name FROM sqlite_master WHERE name='participant_metrics'").fetchone()
    c2.close()


def test_pool_default_empty(conn):
    assert db.get_pool(conn) == {"main_blind": None, "core": [], "counter": []}


def test_pool_round_trip_and_wholesale_replace(conn):
    db.set_pool(conn, "Gwen", ["Kled", "Garen"], ["Malphite"])
    assert db.get_pool(conn) == {
        "main_blind": "Gwen", "core": ["Kled", "Garen"], "counter": ["Malphite"]}
    db.set_pool(conn, "Kled", [], ["Teemo", "Quinn"])
    assert db.get_pool(conn) == {
        "main_blind": "Kled", "core": [], "counter": ["Teemo", "Quinn"]}


def _seed_block_matches(conn, n):
    ids = []
    for i in range(n):
        match_id = f"EUW1_B{i}"
        db.insert_match(
            conn,
            {"match_id": match_id, "queue_id": 420, "game_creation_ms": 1000 + i,
             "game_duration_s": 1800, "game_version": "x"},
            [make_participant("me", champion_name="Gwen")],
        )
        ids.append(match_id)
    return ids


def test_add_game_auto_advances_blocks(conn):
    ids = _seed_block_matches(conn, 4)
    assert db.add_game_to_block(conn, ids[0], "me") == 1
    assert db.add_game_to_block(conn, ids[1], "me") == 1
    assert db.add_game_to_block(conn, ids[2], "me") == 1
    assert db.add_game_to_block(conn, ids[3], "me") == 2  # 4th game opens block 2
    blocks = db.list_blocks(conn)
    assert [b["id"] for b in blocks] == [2, 1]  # newest first


def test_add_duplicate_game_raises_and_is_findable(conn):
    import sqlite3
    ids = _seed_block_matches(conn, 1)
    db.add_game_to_block(conn, ids[0], "me")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_game_to_block(conn, ids[0], "me")
    assert db.find_block_for_game(conn, ids[0], "me") == 1
    assert db.find_block_for_game(conn, "EUW1_none", "me") is None


def test_record_rank_history_appends_and_ignores_same_ms(conn):
    db.record_rank_history(conn, "p1", "GOLD", "II", 40, 1000)
    db.record_rank_history(conn, "p1", "GOLD", "II", 40, 1000)  # same-ms duplicate
    db.record_rank_history(conn, "p1", "GOLD", "II", 55, 2000)
    rows = conn.execute("SELECT * FROM rank_history ORDER BY fetched_at_ms").fetchall()
    assert [(r["solo_lp"], r["fetched_at_ms"]) for r in rows] == [(40, 1000), (55, 2000)]


def test_seed_rank_history_backfills_from_snapshots(tmp_path):
    c = db.connect(tmp_path / "x.sqlite")
    db.upsert_player(c, "p1", "PlayerOne", "EUW", is_tracked=True)
    c.execute("UPDATE players SET solo_tier='PLATINUM', solo_division='II', solo_lp=45,"
              " rank_fetched_at_ms=5000 WHERE puuid='p1'")
    c.commit()
    db.add_session(c, "2026-07-05", "t")  # captures start_ranks at created_at_ms
    assert not c.execute("SELECT 1 FROM rank_history").fetchone()
    c.close()
    c = db.connect(tmp_path / "x.sqlite")  # reconnect: empty table -> seeded
    rows = c.execute("SELECT * FROM rank_history ORDER BY fetched_at_ms").fetchall()
    assert len(rows) == 2  # session snapshot + current players rank
    assert all(r["puuid"] == "p1" and r["solo_tier"] == "PLATINUM" for r in rows)
    assert rows[0]["fetched_at_ms"] == 5000  # players.rank_fetched_at_ms
    c.close()
    c = db.connect(tmp_path / "x.sqlite")  # non-empty -> seed skipped, no dupes
    assert c.execute("SELECT COUNT(*) n FROM rank_history").fetchone()["n"] == 2
    c.close()


def test_session_captures_tracked_ranks_at_creation(conn):
    import json
    db.upsert_player(conn, "p1", "PlayerOne", "EUW", is_tracked=True)
    conn.execute("UPDATE players SET solo_tier='PLATINUM', solo_division='II', solo_lp=45"
                 " WHERE puuid='p1'")
    conn.commit()
    db.add_session(conn, "2026-07-05", "t")
    row = db.list_sessions(conn)[0]
    ranks = json.loads(row["start_ranks"])
    assert ranks == [{"account": "PlayerOne#EUW", "tier": "PLATINUM",
                      "division": "II", "lp": 45}]


def test_block_captures_start_and_end_ranks(conn):
    import json
    db.upsert_player(conn, "me", "PlayerOne", "EUW", is_tracked=True)
    conn.execute("UPDATE players SET solo_tier='PLATINUM', solo_division='II', solo_lp=40"
                 " WHERE puuid='me'")
    conn.commit()
    ids = _seed_block_matches(conn, 3)
    db.add_game_to_block(conn, ids[0], "me")
    block = db.list_blocks(conn)[0]
    assert json.loads(block["start_ranks"])[0]["lp"] == 40
    assert block["end_ranks"] is None
    # LP changes before the block completes
    conn.execute("UPDATE players SET solo_lp=67 WHERE puuid='me'")
    conn.commit()
    db.add_game_to_block(conn, ids[1], "me")
    db.add_game_to_block(conn, ids[2], "me")  # completes the block
    block = db.list_blocks(conn)[0]
    assert json.loads(block["start_ranks"])[0]["lp"] == 40
    assert json.loads(block["end_ranks"])[0]["lp"] == 67


def test_rank_columns_migrate_onto_legacy_tables(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute("""CREATE TABLE coaching_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_date TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
        created_at_ms INTEGER)""")
    legacy.execute("""CREATE TABLE blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '',
        learnings TEXT NOT NULL DEFAULT '', pool_snapshot TEXT, created_at_ms INTEGER)""")
    legacy.execute("INSERT INTO coaching_sessions (session_date) VALUES ('2026-06-28')")
    legacy.execute("INSERT INTO blocks (created_at_ms) VALUES (1)")
    legacy.commit()
    legacy.close()
    conn = db.connect(path)
    assert db.list_sessions(conn)[0]["start_ranks"] is None
    block = db.list_blocks(conn)[0]
    assert block["start_ranks"] is None and block["end_ranks"] is None
    conn.close()


def test_completing_block_snapshots_current_pool(conn):
    import json
    db.set_pool(conn, "Gwen", ["Kled"], ["Quinn"])
    ids = _seed_block_matches(conn, 4)
    for i in range(3):
        db.add_game_to_block(conn, ids[i], "me")
    block = db.list_blocks(conn)[0]
    assert json.loads(block["pool_snapshot"]) == {
        "main_blind": "Gwen", "core": ["Kled"], "counter": ["Quinn"]}
    # pool changes afterwards don't rewrite the snapshot
    db.set_pool(conn, "Kled", [], [])
    db.add_game_to_block(conn, ids[3], "me")  # opens block 2, still open
    blocks = db.list_blocks(conn)
    assert json.loads(blocks[1]["pool_snapshot"])["main_blind"] == "Gwen"
    assert blocks[0]["pool_snapshot"] is None  # open block: no snapshot yet


def test_snapshot_pool_to_block_stamps_only_when_missing(conn):
    import json
    db.set_pool(conn, "Gwen", [], [])
    ids = _seed_block_matches(conn, 1)
    block_id = db.add_game_to_block(conn, ids[0], "me")
    assert db.snapshot_pool_to_block(conn, block_id) is True
    db.set_pool(conn, "Kled", [], [])
    assert db.snapshot_pool_to_block(conn, block_id) is False  # already stamped
    block = db.list_blocks(conn)[0]
    assert json.loads(block["pool_snapshot"])["main_blind"] == "Gwen"


def test_pool_snapshot_column_added_to_existing_blocks_table(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.execute("""CREATE TABLE blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT '', learnings TEXT NOT NULL DEFAULT '',
        created_at_ms INTEGER)""")
    legacy.execute("INSERT INTO blocks (created_at_ms) VALUES (1)")
    legacy.commit()
    legacy.close()
    conn = db.connect(path)
    assert db.list_blocks(conn)[0]["pool_snapshot"] is None
    conn.close()


def test_block_update_and_game_notes_and_deletes(conn):
    ids = _seed_block_matches(conn, 2)
    block_id = db.add_game_to_block(conn, ids[0], "me")
    db.add_game_to_block(conn, ids[1], "me")
    assert db.update_block(conn, block_id, title="Kled block", learnings="## Learned") is True
    assert db.update_block(conn, 999, title="x") is False
    block = db.list_blocks(conn)[0]
    assert block["title"] == "Kled block"
    entry_id = conn.execute("SELECT id FROM block_games LIMIT 1").fetchone()["id"]
    assert db.update_block_game(conn, entry_id, "froze wave well") is True
    assert conn.execute("SELECT notes FROM block_games WHERE id=?", (entry_id,)
                        ).fetchone()["notes"] == "froze wave well"
    assert db.delete_block_game(conn, entry_id) is True
    assert db.delete_block_game(conn, entry_id) is False
    # cascade delete
    assert db.delete_block(conn, block_id) is True
    assert conn.execute("SELECT COUNT(*) c FROM block_games").fetchone()["c"] == 0


def test_crawl_watermark_round_trip(conn):
    assert db.get_crawl_watermark(conn, "pu1", 420) == (None, False)
    db.set_crawl_watermark(conn, "pu1", 420, newest_ms=123, complete=False)
    assert db.get_crawl_watermark(conn, "pu1", 420) == (123, False)
    db.set_crawl_watermark(conn, "pu1", 420, newest_ms=456, complete=True)
    assert db.get_crawl_watermark(conn, "pu1", 420) == (456, True)
    # different queue independent
    assert db.get_crawl_watermark(conn, "pu1", 440) == (None, False)


def test_matchup_notes_roundtrip(conn):
    assert db.get_matchup_notes(conn) == {}
    db.set_matchup_note(conn, "Darius", "- care ghost timings")
    db.set_matchup_note(conn, "Teemo", "ban it")
    assert db.get_matchup_notes(conn) == {
        "Darius": "- care ghost timings", "Teemo": "ban it"}
    db.set_matchup_note(conn, "Darius", "updated")
    assert db.get_matchup_notes(conn)["Darius"] == "updated"
    db.set_matchup_note(conn, "Teemo", "  ")  # blank deletes
    assert "Teemo" not in db.get_matchup_notes(conn)
