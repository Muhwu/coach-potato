import sqlite3

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


def test_new_blocks_join_the_current_series(conn):
    ids = _seed_block_matches(conn, 2)
    db.add_game_to_block(conn, ids[0], "me")  # block 1, default series
    default_sid = db.current_series_id(conn)
    new_sid = db.start_new_series(conn, "Playoffs")
    assert new_sid != default_sid
    # block 1 had a game -> finalized under the old series; next game starts a
    # new block under the new series
    b2 = db.add_game_to_block(conn, ids[1], "me")
    row = conn.execute("SELECT series_id FROM blocks WHERE id=?", (b2,)).fetchone()
    assert row["series_id"] == new_sid


def test_start_new_series_moves_empty_open_block(conn):
    db.create_block(conn)  # empty open block in the default series
    default_sid = db.current_series_id(conn)
    new_sid = db.start_new_series(conn, "Fresh")
    assert new_sid != default_sid
    # the empty open block is pulled into the new series rather than stranded
    row = conn.execute("SELECT series_id FROM blocks").fetchone()
    assert row["series_id"] == new_sid


def test_seed_block_series_backfills_legacy_blocks(tmp_path):
    """Upgrading a db whose blocks predate series: every block must be assigned
    to a default series so indexing/labels work."""
    path = tmp_path / "pre_series.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE blocks (id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT '', learnings TEXT NOT NULL DEFAULT '',
        pool_snapshot TEXT, start_ranks TEXT, end_ranks TEXT,
        closed_at_ms INTEGER, created_at_ms INTEGER)""")
    raw.execute("INSERT INTO blocks (title) VALUES ('old block')")
    raw.commit()
    raw.close()
    c = db.connect(path)  # _migrate adds series_id, seed_block_series assigns it
    row = c.execute("SELECT series_id FROM blocks").fetchone()
    assert row["series_id"] is not None
    assert c.execute("SELECT COUNT(*) n FROM block_series").fetchone()["n"] == 1
    c.close()


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


def test_matchup_notes_roundtrip(conn):
    assert db.get_matchup_notes(conn, "Gwen") == {}
    db.set_matchup_note(conn, "Gwen", "Darius", notes="- care ghost timings",
                         runes=[CONQ_PAGE], patch_version="14.14")
    db.set_matchup_note(conn, "Gwen", "Teemo", notes="ban it")
    assert db.get_matchup_notes(conn, "Gwen") == {
        "Darius": {"notes": "- care ghost timings", "runes": [CONQ_PAGE],
                    "patch_version": "14.14", "skill_order": []},
        "Teemo": {"notes": "ban it", "runes": [], "patch_version": "", "skill_order": []},
    }
    # a different "my champion" has its own, independent guide for the same opponent
    db.set_matchup_note(conn, "Camille", "Darius", notes="camille vs darius is easier")
    assert db.get_matchup_notes(conn, "Camille") == {
        "Darius": {"notes": "camille vs darius is easier", "runes": [],
                    "patch_version": "", "skill_order": []}}
    assert db.get_matchup_notes(conn, "Gwen")["Darius"]["notes"] == "- care ghost timings"
    # partial update: fields not passed keep their stored value
    db.set_matchup_note(conn, "Gwen", "Darius", notes="updated")
    assert db.get_matchup_notes(conn, "Gwen")["Darius"]["notes"] == "updated"
    assert db.get_matchup_notes(conn, "Gwen")["Darius"]["runes"] == [CONQ_PAGE]
    # a matchup can carry more than one rune page (e.g. alternatives being tested)
    db.set_matchup_note(conn, "Gwen", "Renekton", runes=[CONQ_PAGE, GRASP_PAGE])
    assert db.get_matchup_notes(conn, "Gwen")["Renekton"]["runes"] == [CONQ_PAGE, GRASP_PAGE]
    # skill order saves alone without touching the rest, and clears alone too
    order = ["Q", "W", "E", "Q", "Q", "R"] + [""] * 12
    db.set_matchup_note(conn, "Gwen", "Darius", skill_order=order)
    darius = db.get_matchup_notes(conn, "Gwen")["Darius"]
    assert darius["skill_order"] == order
    assert darius["notes"] == "updated"
    db.set_matchup_note(conn, "Gwen", "Darius", skill_order=[])
    assert db.get_matchup_notes(conn, "Gwen")["Darius"]["skill_order"] == []
    # a row whose only content was a build is deleted when the build clears
    db.set_matchup_note(conn, "Gwen", "Aatrox", skill_order=order)
    db.set_matchup_note(conn, "Gwen", "Aatrox", skill_order=[])
    assert "Aatrox" not in db.get_matchup_notes(conn, "Gwen")
    db.set_matchup_note(conn, "Gwen", "Teemo", notes="  ")  # notes-only row: blank deletes
    assert "Teemo" not in db.get_matchup_notes(conn, "Gwen")


def test_matchup_notes_pk_migration_preserves_data(tmp_path):
    """Upgrading from the original pre-champ-guide schema (opp_champion-only
    PK, no my_champion or rune columns at all) must preserve existing notes —
    moved to my_champion='' since the old schema didn't track which champion
    they were written for."""
    path = tmp_path / "pre_guide.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE matchup_notes (
        opp_champion TEXT PRIMARY KEY,
        notes TEXT NOT NULL DEFAULT '',
        updated_at_ms INTEGER
    )""")
    raw.execute("INSERT INTO matchup_notes (opp_champion, notes, updated_at_ms) "
                "VALUES ('Darius', 'old note', 1700000000000)")
    raw.commit()
    raw.close()
    c = db.connect(path)  # "upgrade": _migrate rebuilds the table with the new PK
    assert db.get_matchup_notes(c, "")["Darius"]["notes"] == "old note"
    c.close()


def test_matchup_notes_migration_from_single_keystone_shape(tmp_path):
    """Upgrading from the v1.13.0 shape (opp_champion-only PK, separate
    primary_keystone/secondary_tree columns instead of a runes-list JSON
    blob) must fold the old single keystone/tree pick into a one-page runes
    list, still scoped to my_champion=''."""
    path = tmp_path / "v1_13_0.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE matchup_notes (
        opp_champion TEXT PRIMARY KEY,
        notes TEXT NOT NULL DEFAULT '',
        primary_keystone TEXT NOT NULL DEFAULT '',
        secondary_tree TEXT NOT NULL DEFAULT '',
        patch_version TEXT NOT NULL DEFAULT '',
        updated_at_ms INTEGER
    )""")
    raw.execute("""INSERT INTO matchup_notes
        (opp_champion, notes, primary_keystone, secondary_tree, patch_version, updated_at_ms)
        VALUES ('Darius', 'old note', 'Conqueror', 'Resolve', '14.14', 1700000000000)""")
    raw.commit()
    raw.close()
    c = db.connect(path)
    guide = db.get_matchup_notes(c, "")["Darius"]
    assert guide["notes"] == "old note"
    assert guide["patch_version"] == "14.14"
    assert guide["runes"] == [{
        "label": "", "primary_tree": "", "keystone": "Conqueror",
        "primary_runes": [], "secondary_tree": "Resolve", "secondary_runes": [], "shards": [],
    }]
    c.close()


def test_matchup_notes_skill_order_column_migration(tmp_path):
    """Upgrading from the v1.14-v1.31 shape (my_champion PK, no skill_order
    column) must add the column and preserve existing rows."""
    path = tmp_path / "v1_31.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE matchup_notes (
        my_champion TEXT NOT NULL,
        opp_champion TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        runes TEXT NOT NULL DEFAULT '',
        patch_version TEXT NOT NULL DEFAULT '',
        updated_at_ms INTEGER,
        PRIMARY KEY (my_champion, opp_champion)
    )""")
    raw.execute("INSERT INTO matchup_notes (my_champion, opp_champion, notes) "
                "VALUES ('Gwen', 'Darius', 'keep me')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    guide = db.get_matchup_notes(c, "Gwen")["Darius"]
    assert guide["notes"] == "keep me"
    assert guide["skill_order"] == []
    order = ["Q"] + [""] * 17
    db.set_matchup_note(c, "Gwen", "Darius", skill_order=order)
    assert db.get_matchup_notes(c, "Gwen")["Darius"]["skill_order"] == order
    c.close()


def test_champion_notes_gains_runes_column_on_upgrade(tmp_path):
    """Upgrading from a champion_notes table without the general-runes column
    (pre-runes_mode) must add it and preserve existing notes."""
    path = tmp_path / "pre_runes.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE champion_notes (
        champion TEXT PRIMARY KEY,
        notes TEXT NOT NULL DEFAULT '',
        updated_at_ms INTEGER
    )""")
    raw.execute("INSERT INTO champion_notes (champion, notes) VALUES ('Gwen', 'keep me')")
    raw.commit()
    raw.close()
    c = db.connect(path)  # _migrate adds the runes column
    assert db.get_champion_note(c, "Gwen") == "keep me"
    assert db.get_champion_runes(c, "Gwen") == ""
    db.set_champion_runes(c, "Gwen", '[{"keystone": "Conqueror"}]')
    assert "Conqueror" in db.get_champion_runes(c, "Gwen")
    assert db.get_champion_note(c, "Gwen") == "keep me"  # notes untouched
    c.close()


def test_participant_metrics_gains_new_columns_on_upgrade(tmp_path):
    """Adding a metric to the registry must additively grow an existing
    participant_metrics table (CREATE TABLE IF NOT EXISTS won't) and add the
    has_timeline flag, without dropping stored rows."""
    path = tmp_path / "old_metrics.sqlite"
    raw = sqlite3.connect(path)
    # a minimal pre-timeline metrics table: only a couple of columns, no
    # has_timeline, no lane-delta columns
    raw.execute("""CREATE TABLE participant_metrics (
        match_id TEXT NOT NULL, puuid TEXT NOT NULL,
        has_challenges INTEGER NOT NULL DEFAULT 0, cs_at_10 REAL,
        PRIMARY KEY (match_id, puuid))""")
    raw.execute("INSERT INTO participant_metrics (match_id, puuid, has_challenges, cs_at_10) "
                "VALUES ('M1', 'p1', 1, 80)")
    raw.commit()
    raw.close()
    c = db.connect(path)  # _migrate adds the missing columns
    cols = {r["name"] for r in c.execute("PRAGMA table_info(participant_metrics)")}
    assert "has_timeline" in cols
    assert {"cs_diff_7", "level_diff_14", "gold_diff_7"} <= cols
    row = c.execute("SELECT cs_at_10, has_timeline, cs_diff_7 FROM participant_metrics").fetchone()
    assert row["cs_at_10"] == 80  # preserved
    assert row["has_timeline"] == 0
    assert row["cs_diff_7"] is None
    c.close()


def test_champion_note_roundtrip(conn):
    assert db.get_champion_note(conn, "Gwen") == ""
    db.set_champion_note(conn, "Gwen", "- always take Conqueror\n- build Nashor's first")
    assert db.get_champion_note(conn, "Gwen") == "- always take Conqueror\n- build Nashor's first"
    db.set_champion_note(conn, "Gwen", "updated")
    assert db.get_champion_note(conn, "Gwen") == "updated"
    db.set_champion_note(conn, "Gwen", "  ")  # blank deletes
    assert db.get_champion_note(conn, "Gwen") == ""


def test_item_build_roundtrip(conn):
    assert db.get_item_build(conn, "Gwen") == {"core": [], "situational": []}
    core = ["Riftmaker", "Nashor's Tooth"]
    situational = [{"label": "vs heavy AP", "items": ["Zhonya's Hourglass"]}]
    db.set_item_build(conn, "Gwen", core, situational)
    assert db.get_item_build(conn, "Gwen") == {"core": core, "situational": situational}
    db.set_item_build(conn, "Gwen", ["Riftmaker"], [])
    assert db.get_item_build(conn, "Gwen") == {"core": ["Riftmaker"], "situational": []}
    db.set_item_build(conn, "Gwen", [], [])  # fully blank deletes
    assert db.get_item_build(conn, "Gwen") == {"core": [], "situational": []}
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM champion_item_builds WHERE champion='Gwen'"
    ).fetchone()["c"] == 0


def test_research_entry_crud_and_cascade_delete(conn):
    entry_id = db.create_research_entry(
        conn, "Faker", "Azir", "Zed", "Level 1 pathing", "Interesting recall timing")
    entry = db.get_research_entry(conn, entry_id)
    assert entry["player_name"] == "Faker"
    assert entry["champion"] == "Azir" and entry["opp_champion"] == "Zed"
    assert [dict(r) for r in db.list_research_entries(conn)][0]["id"] == entry_id

    assert db.update_research_entry(
        conn, entry_id, "Faker", "Azir", "Zed", "Level 1 pathing", "updated notes") is True
    assert db.get_research_entry(conn, entry_id)["notes"] == "updated notes"
    assert db.update_research_entry(conn, 999, "x", "", "", "", "") is False

    shot_id = db.add_research_screenshot(conn, entry_id, "level 1 setup", "abc.png")
    assert db.get_research_screenshot(conn, shot_id)["caption"] == "level 1 setup"
    assert len(db.list_research_screenshots(conn, entry_id)) == 1

    assert db.delete_research_entry(conn, entry_id) is True
    assert db.get_research_entry(conn, entry_id) is None
    assert db.list_research_screenshots(conn, entry_id) == []
    assert db.delete_research_entry(conn, 999) is False


def test_close_block_early_and_next_game_starts_new_block(conn):
    ids = _seed_block_matches(conn, 3)
    assert db.add_game_to_block(conn, ids[0], "me") == 1
    assert db.close_block(conn, 1) is True
    row = conn.execute("SELECT closed_at_ms, pool_snapshot, end_ranks FROM blocks WHERE id=1").fetchone()
    assert row["closed_at_ms"] is not None
    assert row["end_ranks"] is not None  # snapshot stamped like a full block
    assert db.close_block(conn, 1) is False   # already closed
    assert db.close_block(conn, 999) is False # missing
    assert db.add_game_to_block(conn, ids[1], "me") == 2  # closed block skipped


def test_close_block_refused_when_naturally_complete(conn):
    ids = _seed_block_matches(conn, 3)
    for match_id in ids:
        db.add_game_to_block(conn, match_id, "me")
    assert db.close_block(conn, 1) is False


def test_upgrade_from_older_db_preserves_all_notes(tmp_path):
    """Simulate an app upgrade: reconnecting to an existing db must never
    clear user content (sessions, block notes, learnings, matchup notes,
    champion notes)."""
    path = tmp_path / "old.sqlite"
    c = db.connect(path)
    db.upsert_player(c, "p1", "PlayerOne", "EUW", is_tracked=True)
    db.add_session(c, "2026-07-01", "waves", notes="# keep me")
    ids = _seed_block_matches(c, 1)
    db.add_game_to_block(c, ids[0], "me")
    entry = c.execute("SELECT id FROM block_games").fetchone()["id"]
    db.update_block_game(c, entry, "game note")
    db.update_block(c, 1, learnings="learned things")
    db.set_matchup_note(c, "Gwen", "Darius", notes="matchup note", runes=[CONQ_PAGE])
    db.set_champion_note(c, "Gwen", "general champion note")
    db.set_item_build(c, "Gwen", ["Riftmaker"], [{"label": "vs AP", "items": ["Zhonya's Hourglass"]}])
    research_id = db.create_research_entry(c, "Faker", "Azir", "Zed", "Level 1", "keep this too")
    # drop a column added by a later version to mimic an older schema
    c.execute("ALTER TABLE blocks DROP COLUMN closed_at_ms")
    c.commit()
    c.close()
    c = db.connect(path)  # "upgrade": _migrate + SCHEMA re-run
    assert db.list_sessions(c)[0]["notes"] == "# keep me"
    assert c.execute("SELECT notes FROM block_games").fetchone()["notes"] == "game note"
    assert c.execute("SELECT learnings FROM blocks").fetchone()["learnings"] == "learned things"
    assert db.get_matchup_notes(c, "Gwen") == {"Darius": {
        "notes": "matchup note", "runes": [CONQ_PAGE], "patch_version": "",
        "skill_order": []}}
    assert db.get_champion_note(c, "Gwen") == "general champion note"
    assert db.get_item_build(c, "Gwen") == {
        "core": ["Riftmaker"], "situational": [{"label": "vs AP", "items": ["Zhonya's Hourglass"]}]}
    assert db.get_research_entry(c, research_id)["notes"] == "keep this too"
    assert c.execute("SELECT closed_at_ms FROM blocks").fetchone()["closed_at_ms"] is None
    c.close()


def test_block_size_setting_clamped_and_respected(conn):
    assert db.get_block_size(conn) == 3  # default
    db.set_settings(conn, {"block_size": "2"})
    assert db.get_block_size(conn) == 2
    db.set_settings(conn, {"block_size": "99"})
    assert db.get_block_size(conn) == 99  # no upper bound
    db.set_settings(conn, {"block_size": "0"})
    assert db.get_block_size(conn) == 1  # floored at 1
    db.set_settings(conn, {"block_size": "junk"})
    assert db.get_block_size(conn) == 3
    # auto-advance follows the setting
    db.set_settings(conn, {"block_size": "2"})
    ids = _seed_block_matches(conn, 3)
    assert db.add_game_to_block(conn, ids[0], "me") == 1
    assert db.add_game_to_block(conn, ids[1], "me") == 1  # fills block of 2
    assert db.add_game_to_block(conn, ids[2], "me") == 2


def test_raising_block_size_does_not_reopen_finalized_block(conn):
    ids = _seed_block_matches(conn, 4)
    for match_id in ids[:3]:
        db.add_game_to_block(conn, match_id, "me")  # block 1 finalized at 3
    db.set_settings(conn, {"block_size": "5"})
    assert db.add_game_to_block(conn, ids[3], "me") == 2  # new block, not reopened
    assert db.close_block(conn, 1) is False  # finalized blocks can't be closed


def _match_at(conn, match_id, t_ms):
    db.insert_match(conn, {"match_id": match_id, "queue_id": 420,
                           "game_creation_ms": t_ms, "game_duration_s": 1800,
                           "game_version": "x"},
                    [make_participant("me")])


def test_block_gap_exceeded_detects_time_gap(conn):
    H = 3_600_000
    _match_at(conn, "G1", 10 * H)
    _match_at(conn, "G2", 12 * H)   # 2 h later: within default 3 h
    _match_at(conn, "G3", 16 * H)   # 6 h later: beyond
    _match_at(conn, "G0", 2 * H)    # 8 h older: beyond (absolute gap)
    assert db.block_gap_exceeded(conn, "G1") is None  # no open block yet
    db.add_game_to_block(conn, "G1", "me")
    assert db.block_gap_exceeded(conn, "G2") is None
    assert db.block_gap_exceeded(conn, "G3") == (1, 6 * H)
    assert db.block_gap_exceeded(conn, "G0") == (1, 8 * H)
    db.set_settings(conn, {"block_gap_hours": "10"})  # raise threshold
    assert db.block_gap_exceeded(conn, "G3") is None
    db.set_settings(conn, {"block_gap_hours": "0"})   # disabled
    assert db.block_gap_exceeded(conn, "G0") is None
