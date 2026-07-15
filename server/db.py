"""SQLite storage layer. All timestamps are ms epoch (Riot convention)."""
import json
import sqlite3
from pathlib import Path

from .metrics import metric_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    game_name TEXT,
    tag_line TEXT,
    is_tracked INTEGER NOT NULL DEFAULT 0,
    solo_tier TEXT,
    solo_division TEXT,
    solo_lp INTEGER,
    rank_fetched_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    queue_id INTEGER NOT NULL,
    game_creation_ms INTEGER NOT NULL,
    game_duration_s INTEGER NOT NULL,
    game_version TEXT,
    crawled_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS participants (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    riot_id_name TEXT,
    champion_name TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team_position TEXT,
    win INTEGER NOT NULL,
    kills INTEGER NOT NULL,
    deaths INTEGER NOT NULL,
    assists INTEGER NOT NULL,
    cs INTEGER NOT NULL,
    gold_earned INTEGER NOT NULL,
    damage_to_champions INTEGER NOT NULL,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS idx_participants_puuid ON participants(puuid);

CREATE TABLE IF NOT EXISTS player_ranks (
    puuid TEXT PRIMARY KEY,
    solo_tier TEXT,
    solo_division TEXT,
    solo_lp INTEGER,
    fetched_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS coaching_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    start_ranks TEXT,
    created_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS participant_metrics (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    has_challenges INTEGER NOT NULL DEFAULT 0,
    {metric_columns},
    PRIMARY KEY (match_id, puuid)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS champion_pool (
    role TEXT NOT NULL CHECK (role IN ('main_blind','core','counter')),
    champion TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    learnings TEXT NOT NULL DEFAULT '',
    pool_snapshot TEXT,
    start_ranks TEXT,
    end_ranks TEXT,
    closed_at_ms INTEGER,
    created_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS block_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    added_at_ms INTEGER,
    UNIQUE (match_id, puuid)
);

CREATE TABLE IF NOT EXISTS matchup_notes (
    opp_champion TEXT PRIMARY KEY,
    notes TEXT NOT NULL DEFAULT '',
    primary_keystone TEXT NOT NULL DEFAULT '',
    secondary_tree TEXT NOT NULL DEFAULT '',
    patch_version TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS rank_history (
    puuid TEXT NOT NULL,
    solo_tier TEXT,
    solo_division TEXT,
    solo_lp INTEGER,
    fetched_at_ms INTEGER NOT NULL,
    PRIMARY KEY (puuid, fetched_at_ms)
);

CREATE TABLE IF NOT EXISTS crawl_state (
    puuid TEXT NOT NULL,
    queue_id INTEGER NOT NULL,
    newest_ms INTEGER,
    complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (puuid, queue_id)
);
"""


def connect(db_path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    metric_columns = ",\n    ".join(f"{k} REAL" for k in metric_keys())
    conn.executescript(SCHEMA.format(metric_columns=metric_columns))
    seed_rank_history(conn)
    return conn


def _migrate(conn):
    """In-place upgrades for schema changes on existing databases.
    Missing tables are skipped — SCHEMA creates them in their current shape."""
    session_columns = {r["name"] for r in conn.execute("PRAGMA table_info(coaching_sessions)")}
    if session_columns:
        if "note" in session_columns and "title" not in session_columns:
            conn.execute("ALTER TABLE coaching_sessions RENAME COLUMN note TO title")
        if "notes" not in session_columns:
            conn.execute(
                "ALTER TABLE coaching_sessions ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
    if session_columns and "start_ranks" not in session_columns:
        conn.execute("ALTER TABLE coaching_sessions ADD COLUMN start_ranks TEXT")
    block_columns = {r["name"] for r in conn.execute("PRAGMA table_info(blocks)")}
    if block_columns:
        if "pool_snapshot" not in block_columns:
            conn.execute("ALTER TABLE blocks ADD COLUMN pool_snapshot TEXT")
        if "start_ranks" not in block_columns:
            conn.execute("ALTER TABLE blocks ADD COLUMN start_ranks TEXT")
        if "end_ranks" not in block_columns:
            conn.execute("ALTER TABLE blocks ADD COLUMN end_ranks TEXT")
        if "closed_at_ms" not in block_columns:
            conn.execute("ALTER TABLE blocks ADD COLUMN closed_at_ms INTEGER")
    matchup_notes_columns = {r["name"] for r in conn.execute("PRAGMA table_info(matchup_notes)")}
    if matchup_notes_columns:
        if "primary_keystone" not in matchup_notes_columns:
            conn.execute(
                "ALTER TABLE matchup_notes ADD COLUMN primary_keystone TEXT NOT NULL DEFAULT ''")
        if "secondary_tree" not in matchup_notes_columns:
            conn.execute(
                "ALTER TABLE matchup_notes ADD COLUMN secondary_tree TEXT NOT NULL DEFAULT ''")
        if "patch_version" not in matchup_notes_columns:
            conn.execute(
                "ALTER TABLE matchup_notes ADD COLUMN patch_version TEXT NOT NULL DEFAULT ''")
    conn.commit()


def has_match(conn, match_id: str) -> bool:
    return conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is not None


def insert_match(conn, match_row: dict, participant_rows: list) -> bool:
    """Insert a match with its participants in one transaction.

    Returns False (no-op) if the match is already stored.
    """
    if has_match(conn, match_row["match_id"]):
        return False
    with conn:
        conn.execute(
            """INSERT INTO matches
               (match_id, queue_id, game_creation_ms, game_duration_s, game_version, crawled_at_ms)
               VALUES (:match_id, :queue_id, :game_creation_ms, :game_duration_s, :game_version,
                       CAST(strftime('%s','now') AS INTEGER) * 1000)""",
            match_row,
        )
        conn.executemany(
            """INSERT INTO participants
               (match_id, puuid, riot_id_name, champion_name, team_id, team_position,
                win, kills, deaths, assists, cs, gold_earned, damage_to_champions)
               VALUES (:match_id, :puuid, :riot_id_name, :champion_name, :team_id, :team_position,
                       :win, :kills, :deaths, :assists, :cs, :gold_earned, :damage_to_champions)""",
            [{**p, "match_id": match_row["match_id"]} for p in participant_rows],
        )
    return True


def upsert_player(conn, puuid, game_name, tag_line, is_tracked=False):
    with conn:
        conn.execute(
            """INSERT INTO players (puuid, game_name, tag_line, is_tracked)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(puuid) DO UPDATE SET
                 game_name=excluded.game_name,
                 tag_line=excluded.tag_line,
                 is_tracked=MAX(players.is_tracked, excluded.is_tracked)""",
            (puuid, game_name, tag_line, int(is_tracked)),
        )


def set_player_rank(conn, puuid, tier, division, lp, fetched_at_ms):
    with conn:
        conn.execute(
            """INSERT INTO player_ranks (puuid, solo_tier, solo_division, solo_lp, fetched_at_ms)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(puuid) DO UPDATE SET
                 solo_tier=excluded.solo_tier,
                 solo_division=excluded.solo_division,
                 solo_lp=excluded.solo_lp,
                 fetched_at_ms=excluded.fetched_at_ms""",
            (puuid, tier, division, lp, fetched_at_ms),
        )


def get_player_rank(conn, puuid):
    return conn.execute("SELECT * FROM player_ranks WHERE puuid=?", (puuid,)).fetchone()


def get_matchup_notes(conn):
    """Champ guide (notes, runes, patch) for every matchup with any field set:
    {opp_champion: {notes, primary_keystone, secondary_tree, patch_version}}."""
    rows = conn.execute(
        """SELECT opp_champion, notes, primary_keystone, secondary_tree, patch_version
           FROM matchup_notes
           WHERE notes != '' OR primary_keystone != '' OR secondary_tree != ''
              OR patch_version != ''""")
    return {r["opp_champion"]: {
        "notes": r["notes"], "primary_keystone": r["primary_keystone"],
        "secondary_tree": r["secondary_tree"], "patch_version": r["patch_version"],
    } for r in rows}


def set_matchup_note(conn, champion, notes="", primary_keystone="", secondary_tree="",
                      patch_version=""):
    """Upsert the champ guide for a matchup; all-blank fields delete the row."""
    with conn:
        if not any(f.strip() for f in (notes, primary_keystone, secondary_tree, patch_version)):
            conn.execute("DELETE FROM matchup_notes WHERE opp_champion=?", (champion,))
            return
        conn.execute(
            f"""INSERT INTO matchup_notes
                (opp_champion, notes, primary_keystone, secondary_tree, patch_version,
                 updated_at_ms)
                VALUES (?, ?, ?, ?, ?, {_now_expr()})
                ON CONFLICT(opp_champion) DO UPDATE SET
                  notes=excluded.notes,
                  primary_keystone=excluded.primary_keystone,
                  secondary_tree=excluded.secondary_tree,
                  patch_version=excluded.patch_version,
                  updated_at_ms=excluded.updated_at_ms""",
            (champion, notes, primary_keystone, secondary_tree, patch_version))


def record_rank_history(conn, puuid, tier, division, lp, fetched_at_ms):
    """Append a rank snapshot for a tracked player (same-ms duplicates ignored)."""
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO rank_history
               (puuid, solo_tier, solo_division, solo_lp, fetched_at_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (puuid, tier, division, lp, fetched_at_ms),
        )


def seed_rank_history(conn):
    """One-time backfill of rank_history from snapshots taken before the table
    existed: session/block start_ranks (captured at created_at_ms), block
    end_ranks (~ when the last game was added) and the players' current rank.
    Runs on connect but only while the table is empty."""
    if conn.execute("SELECT 1 FROM rank_history LIMIT 1").fetchone():
        return
    by_account = {f"{r['game_name']}#{r['tag_line']}": r["puuid"]
                  for r in conn.execute(
                      "SELECT puuid, game_name, tag_line FROM players WHERE is_tracked=1")}

    def entries(raw_json, at_ms):
        if not raw_json or not at_ms:
            return
        for entry in json.loads(raw_json):
            puuid = by_account.get(entry.get("account"))
            if puuid and entry.get("tier"):
                record_rank_history(conn, puuid, entry["tier"], entry.get("division"),
                                    entry.get("lp"), at_ms)

    for row in conn.execute("SELECT start_ranks, created_at_ms FROM coaching_sessions"):
        entries(row["start_ranks"], row["created_at_ms"])
    for row in conn.execute(
            """SELECT b.start_ranks, b.end_ranks, b.created_at_ms,
                      (SELECT MAX(added_at_ms) FROM block_games WHERE block_id=b.id) AS last_ms
               FROM blocks b"""):
        entries(row["start_ranks"], row["created_at_ms"])
        entries(row["end_ranks"], row["last_ms"])
    for row in conn.execute(
            """SELECT puuid, solo_tier, solo_division, solo_lp, rank_fetched_at_ms
               FROM players WHERE is_tracked=1 AND solo_tier IS NOT NULL
                 AND rank_fetched_at_ms IS NOT NULL"""):
        record_rank_history(conn, row["puuid"], row["solo_tier"], row["solo_division"],
                            row["solo_lp"], row["rank_fetched_at_ms"])


def insert_participant_metrics(conn, match_id, puuid, values):
    """values: has_challenges + one entry per metric key (None allowed)."""
    columns = ["match_id", "puuid", "has_challenges", *metric_keys()]
    placeholders = ", ".join(f":{c}" for c in columns)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO participant_metrics ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            {**values, "match_id": match_id, "puuid": puuid},
        )


def tracked_ranks(conn):
    """Current solo ranks of all tracked accounts, for rank snapshots."""
    return [
        {"account": f"{r['game_name']}#{r['tag_line']}", "tier": r["solo_tier"],
         "division": r["solo_division"], "lp": r["solo_lp"]}
        for r in conn.execute(
            """SELECT game_name, tag_line, solo_tier, solo_division, solo_lp
               FROM players WHERE is_tracked=1 ORDER BY game_name""")
    ]


def add_session(conn, session_date, title="", notes=""):
    with conn:
        cursor = conn.execute(
            """INSERT INTO coaching_sessions
               (session_date, title, notes, start_ranks, created_at_ms)
               VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)""",
            (session_date, title, notes, json.dumps(tracked_ranks(conn))),
        )
    return cursor.lastrowid


def update_session(conn, session_id, title=None, notes=None):
    """Update the given fields (None = leave unchanged). False if id missing."""
    sets, params = [], []
    if title is not None:
        sets.append("title=?")
        params.append(title)
    if notes is not None:
        sets.append("notes=?")
        params.append(notes)
    if not sets:
        return False
    with conn:
        cursor = conn.execute(
            f"UPDATE coaching_sessions SET {', '.join(sets)} WHERE id=?",
            (*params, session_id),
        )
    return cursor.rowcount > 0


def list_sessions(conn):
    return conn.execute(
        "SELECT * FROM coaching_sessions ORDER BY session_date"
    ).fetchall()


def delete_session(conn, session_id):
    with conn:
        cursor = conn.execute("DELETE FROM coaching_sessions WHERE id=?", (session_id,))
    return cursor.rowcount > 0


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_settings(conn, mapping):
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            list(mapping.items()))


BLOCK_SIZE = 3  # default games per block
MAX_BLOCK_SIZE = 10
BLOCK_GAP_HOURS = 3.0  # default auto-close time gap; 0 disables
MAX_BLOCK_GAP_HOURS = 168.0


def get_block_size(conn):
    """Games per block from settings, clamped to 1..MAX_BLOCK_SIZE."""
    raw = get_settings(conn).get("block_size")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return BLOCK_SIZE
    return max(1, min(MAX_BLOCK_SIZE, size))


def get_block_gap_ms(conn):
    """Auto-close threshold in ms (game-time gap), 0 = disabled."""
    raw = get_settings(conn).get("block_gap_hours")
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        hours = BLOCK_GAP_HOURS
    return int(max(0.0, min(MAX_BLOCK_GAP_HOURS, hours)) * 3_600_000)


def _open_block(conn):
    """Id of the block the next game would land in, or None if a new one
    would open (newest block full, closed early, or finalized)."""
    current = conn.execute("SELECT MAX(id) AS id FROM blocks").fetchone()["id"]
    if current is None:
        return None
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM block_games WHERE block_id=b.id) AS c,
                  b.closed_at_ms, b.pool_snapshot
           FROM blocks b WHERE b.id=?""", (current,)).fetchone()
    if (row["c"] >= get_block_size(conn) or row["closed_at_ms"] is not None
            or row["pool_snapshot"] is not None):
        return None
    return current


def block_gap_exceeded(conn, match_id):
    """(open_block_id, gap_ms) when adding match_id would breach the
    auto-close time gap — blocks are meant to be played in succession, so a
    game far (in game start time) from the open block's latest game closes
    it. None when disabled, no open block, or within the threshold."""
    threshold = get_block_gap_ms(conn)
    if not threshold:
        return None
    current = _open_block(conn)
    if current is None:
        return None
    last = conn.execute(
        """SELECT MAX(m.game_creation_ms) AS t FROM block_games bg
           JOIN matches m ON m.match_id = bg.match_id
           WHERE bg.block_id=?""", (current,)).fetchone()["t"]
    candidate = conn.execute(
        "SELECT game_creation_ms FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if last is None or candidate is None:
        return None
    gap = abs(candidate["game_creation_ms"] - last)
    return (current, gap) if gap > threshold else None


def get_pool(conn):
    rows = conn.execute("SELECT role, champion FROM champion_pool ORDER BY sort").fetchall()
    main = next((r["champion"] for r in rows if r["role"] == "main_blind"), None)
    return {
        "main_blind": main,
        "core": [r["champion"] for r in rows if r["role"] == "core"],
        "counter": [r["champion"] for r in rows if r["role"] == "counter"],
    }


def set_pool(conn, main_blind, core, counter):
    with conn:
        conn.execute("DELETE FROM champion_pool")
        rows = []
        if main_blind:
            rows.append(("main_blind", main_blind, 0))
        rows += [("core", c, i) for i, c in enumerate(core)]
        rows += [("counter", c, i) for i, c in enumerate(counter)]
        conn.executemany(
            "INSERT INTO champion_pool (role, champion, sort) VALUES (?, ?, ?)", rows)


def _now_expr():
    return "CAST(strftime('%s','now') AS INTEGER) * 1000"


def create_block(conn):
    with conn:
        cursor = conn.execute(
            f"INSERT INTO blocks (start_ranks, created_at_ms) VALUES (?, {_now_expr()})",
            (json.dumps(tracked_ranks(conn)),))
    return cursor.lastrowid


def add_game_to_block(conn, match_id, puuid):
    """Add a game to the current (newest) block, opening a new block when the
    current one is full, closed early, already finalized, or absent. Raises
    sqlite3.IntegrityError on duplicates."""
    size = get_block_size(conn)
    # _open_block treats a full, early-closed or finalized (pool_snapshot
    # stamped under an earlier size setting) newest block as unavailable
    current = _open_block(conn)
    if current is None:
        current = create_block(conn)
    with conn:
        conn.execute(
            f"""INSERT INTO block_games (block_id, match_id, puuid, added_at_ms)
                VALUES (?, ?, ?, {_now_expr()})""",
            (current, match_id, puuid),
        )
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM block_games WHERE block_id=?", (current,)
    ).fetchone()["c"]
    if count >= size:
        snapshot_pool_to_block(conn, current)  # pool as committed when finalized
    return current


def _stamp_block_snapshot(conn, block_id):
    """Pool + end-ranks stamp (only once); caller owns the transaction."""
    cursor = conn.execute(
        "UPDATE blocks SET pool_snapshot=? WHERE id=? AND pool_snapshot IS NULL",
        (json.dumps(get_pool(conn)), block_id),
    )
    conn.execute(
        "UPDATE blocks SET end_ranks=? WHERE id=? AND end_ranks IS NULL",
        (json.dumps(tracked_ranks(conn)), block_id),
    )
    return cursor.rowcount > 0


def snapshot_pool_to_block(conn, block_id):
    """Stamp the current pool + end ranks onto a completed block (only once)."""
    with conn:
        return _stamp_block_snapshot(conn, block_id)


def close_block(conn, block_id):
    """Close a block early (irreversible for now). Stamps the pool/end-ranks
    snapshot like a naturally-completed block, in the same transaction.
    Returns False when the block doesn't exist, is empty, or is already
    closed/complete."""
    row = conn.execute(
        """SELECT closed_at_ms, pool_snapshot,
                  (SELECT COUNT(*) FROM block_games WHERE block_id=b.id) AS c
           FROM blocks b WHERE b.id=?""", (block_id,)).fetchone()
    if (row is None or row["closed_at_ms"] is not None
            or row["pool_snapshot"] is not None
            or row["c"] >= get_block_size(conn) or row["c"] == 0):
        return False
    with conn:
        conn.execute(f"UPDATE blocks SET closed_at_ms={_now_expr()} WHERE id=?",
                     (block_id,))
        _stamp_block_snapshot(conn, block_id)
    return True


def find_block_for_game(conn, match_id, puuid):
    row = conn.execute(
        "SELECT block_id FROM block_games WHERE match_id=? AND puuid=?",
        (match_id, puuid)).fetchone()
    return row["block_id"] if row else None


def list_blocks(conn):
    return conn.execute("SELECT * FROM blocks ORDER BY id DESC").fetchall()


def update_block(conn, block_id, title=None, learnings=None):
    sets, params = [], []
    if title is not None:
        sets.append("title=?")
        params.append(title)
    if learnings is not None:
        sets.append("learnings=?")
        params.append(learnings)
    if not sets:
        return False
    with conn:
        cursor = conn.execute(
            f"UPDATE blocks SET {', '.join(sets)} WHERE id=?", (*params, block_id))
    return cursor.rowcount > 0


def update_block_game(conn, entry_id, notes):
    with conn:
        cursor = conn.execute(
            "UPDATE block_games SET notes=? WHERE id=?", (notes, entry_id))
    return cursor.rowcount > 0


def delete_block_game(conn, entry_id):
    with conn:
        cursor = conn.execute("DELETE FROM block_games WHERE id=?", (entry_id,))
    return cursor.rowcount > 0


def delete_block(conn, block_id):
    with conn:
        conn.execute("DELETE FROM block_games WHERE block_id=?", (block_id,))
        cursor = conn.execute("DELETE FROM blocks WHERE id=?", (block_id,))
    return cursor.rowcount > 0


def get_crawl_watermark(conn, puuid, queue_id):
    row = conn.execute(
        "SELECT newest_ms, complete FROM crawl_state WHERE puuid=? AND queue_id=?",
        (puuid, queue_id),
    ).fetchone()
    if row is None:
        return (None, False)
    return (row["newest_ms"], bool(row["complete"]))


def set_crawl_watermark(conn, puuid, queue_id, newest_ms, complete):
    with conn:
        conn.execute(
            """INSERT INTO crawl_state (puuid, queue_id, newest_ms, complete)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(puuid, queue_id) DO UPDATE SET
                 newest_ms=excluded.newest_ms,
                 complete=excluded.complete""",
            (puuid, queue_id, newest_ms, int(complete)),
        )
