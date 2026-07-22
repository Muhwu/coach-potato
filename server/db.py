"""SQLite storage layer. All timestamps are ms epoch (Riot convention)."""
import datetime
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
    has_timeline INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS block_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER,
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
    my_champion TEXT NOT NULL,
    opp_champion TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    runes TEXT NOT NULL DEFAULT '',
    patch_version TEXT NOT NULL DEFAULT '',
    skill_order TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER,
    PRIMARY KEY (my_champion, opp_champion)
);

CREATE TABLE IF NOT EXISTS champion_notes (
    champion TEXT PRIMARY KEY,
    notes TEXT NOT NULL DEFAULT '',
    runes TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS champion_item_builds (
    champion TEXT PRIMARY KEY,
    core TEXT NOT NULL DEFAULT '[]',
    situational TEXT NOT NULL DEFAULT '[]',
    updated_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS participant_runes (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    runes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (match_id, puuid)
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL CHECK (owner_type IN ('session', 'block_game')),
    owner_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('upload', 'link')),
    file_name TEXT,
    url TEXT,
    created_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clips_owner ON clips(owner_type, owner_id);

CREATE TABLE IF NOT EXISTS research_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL DEFAULT '',
    champion TEXT NOT NULL DEFAULT '',
    opp_champion TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER,
    updated_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS research_screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    created_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_research_screenshots_entry ON research_screenshots(entry_id);

CREATE TABLE IF NOT EXISTS comparison_players (
    puuid TEXT PRIMARY KEY,
    game_name TEXT NOT NULL DEFAULT '',
    tag_line TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    lookback_days INTEGER NOT NULL DEFAULT 60,
    sort INTEGER NOT NULL DEFAULT 0,
    added_at_ms INTEGER
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
    seed_block_series(conn)
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
        if "series_id" not in block_columns:  # block series added in v1.40.0
            conn.execute("ALTER TABLE blocks ADD COLUMN series_id INTEGER")
    cp_columns = {r["name"] for r in conn.execute("PRAGMA table_info(comparison_players)")}
    if cp_columns and "platform" not in cp_columns:  # per-player server added later
        conn.execute("ALTER TABLE comparison_players ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
    matchup_notes_columns = {r["name"] for r in conn.execute("PRAGMA table_info(matchup_notes)")}
    if matchup_notes_columns and "my_champion" not in matchup_notes_columns:
        # Pre-v1.14.0 shapes had opp_champion as the sole PK (no per-champion
        # scoping) and, from v1.13.0 on, separate primary_keystone/
        # secondary_tree columns instead of a single runes-list JSON blob.
        # SQLite can't ALTER a primary key, so rebuild the table and copy
        # rows forward: my_champion='' (the old schema didn't track which
        # champion notes were written for), old keystone/tree columns folded
        # into a one-page runes list.
        has_old_runes = "primary_keystone" in matchup_notes_columns
        has_patch = "patch_version" in matchup_notes_columns
        select_cols = "opp_champion, notes, updated_at_ms"
        if has_old_runes:
            select_cols += ", primary_keystone, secondary_tree"
        if has_patch:
            select_cols += ", patch_version"
        old_rows = conn.execute(f"SELECT {select_cols} FROM matchup_notes").fetchall()
        conn.execute("ALTER TABLE matchup_notes RENAME TO matchup_notes_old")
        conn.execute("""
            CREATE TABLE matchup_notes (
                my_champion TEXT NOT NULL,
                opp_champion TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                runes TEXT NOT NULL DEFAULT '',
                patch_version TEXT NOT NULL DEFAULT '',
                skill_order TEXT NOT NULL DEFAULT '',
                updated_at_ms INTEGER,
                PRIMARY KEY (my_champion, opp_champion)
            )""")
        for row in old_rows:
            runes_json = ""
            if has_old_runes and (row["primary_keystone"] or row["secondary_tree"]):
                runes_json = json.dumps([{
                    "label": "", "primary_tree": "", "keystone": row["primary_keystone"] or "",
                    "primary_runes": [], "secondary_tree": row["secondary_tree"] or "",
                    "secondary_runes": [], "shards": [],
                }])
            conn.execute(
                """INSERT INTO matchup_notes
                   (my_champion, opp_champion, notes, runes, patch_version, updated_at_ms)
                   VALUES ('', ?, ?, ?, ?, ?)""",
                (row["opp_champion"], row["notes"], runes_json,
                 row["patch_version"] if has_patch else "", row["updated_at_ms"]))
        conn.execute("DROP TABLE matchup_notes_old")
    elif matchup_notes_columns and "skill_order" not in matchup_notes_columns:
        # v1.14.0..v1.31.x shape — saved skill-order builds added in v1.32.0
        conn.execute(
            "ALTER TABLE matchup_notes ADD COLUMN skill_order TEXT NOT NULL DEFAULT ''")
    cn_columns = {r["name"] for r in conn.execute("PRAGMA table_info(champion_notes)")}
    if cn_columns and "runes" not in cn_columns:  # general (champion-level) runes added with runes_mode
        conn.execute("ALTER TABLE champion_notes ADD COLUMN runes TEXT NOT NULL DEFAULT ''")
    # participant_metrics grows a column whenever a metric is added to the
    # registry (CREATE TABLE IF NOT EXISTS won't touch an existing table).
    # Additively backfill any missing metric columns + the has_timeline flag.
    pm_columns = {r["name"] for r in conn.execute("PRAGMA table_info(participant_metrics)")}
    if pm_columns:
        if "has_timeline" not in pm_columns:
            conn.execute(
                "ALTER TABLE participant_metrics ADD COLUMN has_timeline INTEGER NOT NULL DEFAULT 0")
        for key in metric_keys():
            if key not in pm_columns:
                conn.execute(f"ALTER TABLE participant_metrics ADD COLUMN {key} REAL")
    conn.commit()


def has_match(conn, match_id: str) -> bool:
    return conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is not None


def has_participant(conn, match_id: str, puuid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM participants WHERE match_id=? AND puuid=?",
        (match_id, puuid)).fetchone() is not None


def insert_match(conn, match_row: dict, participant_rows: list) -> bool:
    """Insert a match with its participants in one transaction. Uses INSERT OR
    IGNORE so a match already present (e.g. first crawled via another player,
    or whose participant rows were purged by a since-re-added account) still
    gets any MISSING participant rows backfilled. Returns True if the match row
    itself was newly inserted, False if it already existed (participants may
    still have been added in that case)."""
    is_new = not has_match(conn, match_row["match_id"])
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO matches
               (match_id, queue_id, game_creation_ms, game_duration_s, game_version, crawled_at_ms)
               VALUES (:match_id, :queue_id, :game_creation_ms, :game_duration_s, :game_version,
                       CAST(strftime('%s','now') AS INTEGER) * 1000)""",
            match_row,
        )
        conn.executemany(
            """INSERT OR IGNORE INTO participants
               (match_id, puuid, riot_id_name, champion_name, team_id, team_position,
                win, kills, deaths, assists, cs, gold_earned, damage_to_champions)
               VALUES (:match_id, :puuid, :riot_id_name, :champion_name, :team_id, :team_position,
                       :win, :kills, :deaths, :assists, :cs, :gold_earned, :damage_to_champions)""",
            [{**p, "match_id": match_row["match_id"]} for p in participant_rows],
        )
    return is_new


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


def delete_account_data(conn, puuid):
    """Purge a player's crawled data — participant rows, coaching metrics,
    runes, rank cache/history, crawl watermarks, and the players row itself.
    Shared `matches` rows and user-authored content (blocks, sessions, notes)
    are left untouched; block_games that referenced this puuid simply stop
    hydrating. Re-adding + crawling the account restores it, since Riot's API
    is the source of the crawled data."""
    with conn:
        for tbl in ("participant_metrics", "participant_runes", "participants",
                    "crawl_state", "player_ranks", "rank_history"):
            conn.execute(f"DELETE FROM {tbl} WHERE puuid=?", (puuid,))
        conn.execute("DELETE FROM players WHERE puuid=?", (puuid,))


# ---------- comparison ("research") players: up to N others to compare
# yourself against in the Matchup guide. Stored in their own table (separate
# from tracked `players`) so each can be enabled/disabled independently, on or
# off as you see fit, without touching your own tracked stats. Their match
# data still lands in matches/participants like anyone else; this table just
# records who they are and whether each is currently active. ----------

MAX_COMPARISON_PLAYERS = 5
COMPARISON_LOOKBACK_DAYS = 60  # default fetch window; "Fetch more" extends by this


def list_comparison_players(conn):
    return [dict(r) for r in conn.execute(
        "SELECT puuid, game_name, tag_line, platform, enabled, lookback_days, sort, added_at_ms "
        "FROM comparison_players ORDER BY sort, added_at_ms")]


def comparison_puuids(conn, enabled_only=False):
    sql = "SELECT puuid FROM comparison_players"
    if enabled_only:
        sql += " WHERE enabled=1"
    return [r["puuid"] for r in conn.execute(sql)]


def add_comparison_player(conn, puuid, game_name, tag_line, platform=""):
    """Insert a comparison player (enabled by default). Returns False without
    inserting if the max is already reached (unless this puuid is already one,
    in which case it's a no-op refresh of the display name/server). `platform`
    is the player's server (they may be on a different region than you)."""
    existing = {r["puuid"] for r in conn.execute("SELECT puuid FROM comparison_players")}
    if puuid not in existing and len(existing) >= MAX_COMPARISON_PLAYERS:
        return False
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM comparison_players").fetchone()["n"]
    with conn:
        conn.execute(
            f"""INSERT INTO comparison_players
                  (puuid, game_name, tag_line, platform, lookback_days, sort, added_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, {_now_expr()})
                ON CONFLICT(puuid) DO UPDATE SET
                  game_name=excluded.game_name, tag_line=excluded.tag_line,
                  platform=excluded.platform""",
            (puuid, game_name, tag_line, platform, COMPARISON_LOOKBACK_DAYS, nxt))
    return True


def remove_comparison_player(conn, puuid):
    with conn:
        conn.execute("DELETE FROM comparison_players WHERE puuid=?", (puuid,))


def set_comparison_enabled(conn, puuid, enabled):
    with conn:
        conn.execute("UPDATE comparison_players SET enabled=? WHERE puuid=?",
                     (1 if enabled else 0, puuid))


def bump_comparison_lookback(conn, puuid, extra_days=COMPARISON_LOOKBACK_DAYS):
    """Widen a comparison player's fetch window by extra_days (the "Fetch more"
    action) and return the new lookback_days, or None if unknown."""
    row = conn.execute("SELECT lookback_days FROM comparison_players WHERE puuid=?",
                       (puuid,)).fetchone()
    if row is None:
        return None
    new_days = row["lookback_days"] + extra_days
    with conn:
        conn.execute("UPDATE comparison_players SET lookback_days=? WHERE puuid=?",
                     (new_days, puuid))
    return new_days


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


def get_matchup_notes(conn, my_champion):
    """Champ guide (notes, rune pages, patch, saved skill order) for every
    opponent matchup my_champion has any field set for: {opp_champion:
    {notes, runes: [...], patch_version, skill_order: [...]}}. `runes` is a
    list — a matchup can carry more than one rune page; `skill_order` is up
    to 18 entries of ''/Q/W/E/R (index = level - 1)."""
    rows = conn.execute(
        """SELECT opp_champion, notes, runes, patch_version, skill_order
           FROM matchup_notes
           WHERE my_champion=? AND (notes != '' OR runes != ''
                                    OR patch_version != '' OR skill_order != '')""",
        (my_champion,))
    return {r["opp_champion"]: {
        "notes": r["notes"], "runes": json.loads(r["runes"]) if r["runes"] else [],
        "patch_version": r["patch_version"],
        "skill_order": json.loads(r["skill_order"]) if r["skill_order"] else [],
    } for r in rows}


_KEEP = object()  # set_matchup_note sentinel: leave the stored value alone


def set_matchup_note(conn, my_champion, opp_champion, notes=_KEEP, runes=_KEEP,
                     patch_version=_KEEP, skill_order=_KEEP):
    """Upsert the champ guide for a (my_champion, opp_champion) matchup.
    Fields not passed keep their stored value (so the cooldown popup can save
    just skill_order without touching notes, and vice versa); pass explicit
    blanks to clear. A row whose fields all end up blank is deleted.
    runes: list of rune-page dicts. skill_order: list of ''/Q/W/E/R per level."""
    row = conn.execute(
        """SELECT notes, runes, patch_version, skill_order FROM matchup_notes
           WHERE my_champion=? AND opp_champion=?""",
        (my_champion, opp_champion)).fetchone()
    notes = (row["notes"] if row else "") if notes is _KEEP else (notes or "")
    runes_json = (row["runes"] if row else "") if runes is _KEEP \
        else (json.dumps(runes) if runes else "")
    patch_version = (row["patch_version"] if row else "") if patch_version is _KEEP \
        else (patch_version or "")
    skill_json = (row["skill_order"] if row else "") if skill_order is _KEEP \
        else (json.dumps(skill_order) if skill_order and any(skill_order) else "")
    with conn:
        if (not notes.strip() and not runes_json and not patch_version.strip()
                and not skill_json):
            conn.execute(
                "DELETE FROM matchup_notes WHERE my_champion=? AND opp_champion=?",
                (my_champion, opp_champion))
            return
        conn.execute(
            f"""INSERT INTO matchup_notes
                (my_champion, opp_champion, notes, runes, patch_version, skill_order,
                 updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, {_now_expr()})
                ON CONFLICT(my_champion, opp_champion) DO UPDATE SET
                  notes=excluded.notes,
                  runes=excluded.runes,
                  patch_version=excluded.patch_version,
                  skill_order=excluded.skill_order,
                  updated_at_ms=excluded.updated_at_ms""",
            (my_champion, opp_champion, notes, runes_json, patch_version, skill_json))


def get_champion_note(conn, champion):
    """General (not matchup-specific) Markdown notes for a champion."""
    row = conn.execute(
        "SELECT notes FROM champion_notes WHERE champion=?", (champion,)).fetchone()
    return row["notes"] if row else ""


def _champion_note_runes(conn, champion):
    row = conn.execute("SELECT runes FROM champion_notes WHERE champion=?",
                       (champion,)).fetchone()
    return (row["runes"] if row else "") or ""


def set_champion_note(conn, champion, notes):
    """Upsert a champion's general notes. Blank notes delete the row only if it
    holds no general runes either (runes_mode='general' stores them here)."""
    with conn:
        if not notes.strip():
            if _champion_note_runes(conn, champion) not in ("", "[]"):
                conn.execute(
                    f"UPDATE champion_notes SET notes='', updated_at_ms={_now_expr()} "
                    "WHERE champion=?", (champion,))
            else:
                conn.execute("DELETE FROM champion_notes WHERE champion=?", (champion,))
            return
        conn.execute(
            f"""INSERT INTO champion_notes (champion, notes, updated_at_ms)
                VALUES (?, ?, {_now_expr()})
                ON CONFLICT(champion) DO UPDATE SET
                  notes=excluded.notes, updated_at_ms=excluded.updated_at_ms""",
            (champion, notes))


def get_champion_runes(conn, champion):
    """General (champion-level, not per-matchup) rune pages JSON, or '' if
    none. Used when runes_mode='general' — one rune set shown by the item
    build rather than a set per opponent."""
    return _champion_note_runes(conn, champion)


def set_champion_runes(conn, champion, runes_json):
    """Upsert a champion's general rune pages. Empty runes drop the column,
    deleting the row only if there are no general notes either."""
    with conn:
        if not runes_json or runes_json == "[]":
            row = conn.execute("SELECT notes FROM champion_notes WHERE champion=?",
                               (champion,)).fetchone()
            if row and row["notes"].strip():
                conn.execute(
                    f"UPDATE champion_notes SET runes='', updated_at_ms={_now_expr()} "
                    "WHERE champion=?", (champion,))
            else:
                conn.execute("DELETE FROM champion_notes WHERE champion=?", (champion,))
            return
        conn.execute(
            f"""INSERT INTO champion_notes (champion, notes, runes, updated_at_ms)
                VALUES (?, '', ?, {_now_expr()})
                ON CONFLICT(champion) DO UPDATE SET
                  runes=excluded.runes, updated_at_ms=excluded.updated_at_ms""",
            (champion, runes_json))


def get_item_build(conn, champion):
    """Item build for a champion: a core (ordered) list of item names, plus
    labeled situational sections (each its own small item list) — e.g.
    "vs heavy AP": ["Item A", "Item B"]. Empty defaults when none recorded."""
    row = conn.execute(
        "SELECT core, situational FROM champion_item_builds WHERE champion=?",
        (champion,)).fetchone()
    if not row:
        return {"core": [], "situational": []}
    return {"core": json.loads(row["core"]), "situational": json.loads(row["situational"])}


def set_item_build(conn, champion, core, situational):
    """Upsert a champion's item build; fully blank (no core items, no
    situational sections) deletes the row."""
    with conn:
        if not core and not situational:
            conn.execute("DELETE FROM champion_item_builds WHERE champion=?", (champion,))
            return
        conn.execute(
            f"""INSERT INTO champion_item_builds (champion, core, situational, updated_at_ms)
                VALUES (?, ?, ?, {_now_expr()})
                ON CONFLICT(champion) DO UPDATE SET
                  core=excluded.core, situational=excluded.situational,
                  updated_at_ms=excluded.updated_at_ms""",
            (champion, json.dumps(core), json.dumps(situational)))


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
    """values: has_challenges (+ optional has_timeline) + one entry per metric
    key (None allowed). INSERT OR REPLACE rewrites the whole row."""
    columns = ["match_id", "puuid", "has_challenges", "has_timeline", *metric_keys()]
    row = {"has_timeline": 0, **values, "match_id": match_id, "puuid": puuid}
    placeholders = ", ".join(f":{c}" for c in columns)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO participant_metrics ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            row,
        )


def update_participant_timeline(conn, match_id, puuid, deltas):
    """Update only the timeline-derived delta columns (+ has_timeline flag) on
    an existing metrics row, leaving challenge/participant metrics intact. Used
    by the timeline backfill, which must not clobber already-stored metrics."""
    keys = list(deltas)
    assignments = ", ".join(f"{k}=:{k}" for k in keys)
    with conn:
        conn.execute(
            f"UPDATE participant_metrics SET has_timeline=1{', ' + assignments if keys else ''} "
            f"WHERE match_id=:match_id AND puuid=:puuid",
            {**deltas, "match_id": match_id, "puuid": puuid},
        )


def insert_participant_runes(conn, match_id, puuid, runes):
    """runes: a decoded rune-page dict (server.rune_data.decode_perks), or
    None when the match had no usable perks data. Always inserts a row
    (blank when None) so backfill_runes doesn't keep re-fetching matches
    that genuinely have no perks data."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO participant_runes (match_id, puuid, runes) VALUES (?, ?, ?)",
            (match_id, puuid, json.dumps(runes) if runes else ""))


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
BLOCK_GAP_HOURS = 3.0  # default auto-close time gap; 0 disables
MAX_BLOCK_GAP_HOURS = 168.0


def get_block_size(conn):
    """Games per block from settings, floored at 1 (no upper bound)."""
    raw = get_settings(conn).get("block_size")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return BLOCK_SIZE
    return max(1, size)


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


# ---------- block series (named groupings, e.g. a 2-week challenge) ----------

def _generated_series_title():
    d = datetime.date.today()
    return f"Since {d.month}/{d.day}/{d.year}"


def create_block_series(conn, title=None):
    with conn:
        cursor = conn.execute(
            f"INSERT INTO block_series (title, created_at_ms) VALUES (?, {_now_expr()})",
            (title if title else _generated_series_title(),))
    return cursor.lastrowid


def current_series_id(conn):
    """Newest series' id, creating a default one if none exists yet."""
    row = conn.execute("SELECT id FROM block_series ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else create_block_series(conn)


def seed_block_series(conn):
    """Ensure a series exists and every block belongs to one — runs on connect
    (idempotent). First upgrade: all existing blocks join the default series."""
    sid = current_series_id(conn)
    with conn:
        conn.execute("UPDATE blocks SET series_id=? WHERE series_id IS NULL", (sid,))


def start_new_series(conn, title=None):
    """Begin a fresh series so subsequent blocks number from #1 under it. Any
    in-progress (open) block is finalized if it has games, or moved into the
    new series if empty, so the next game starts the new series cleanly."""
    open_block = _open_block(conn)
    new_sid = create_block_series(conn, title)
    if open_block is not None:
        count = conn.execute(
            "SELECT COUNT(*) c FROM block_games WHERE block_id=?", (open_block,)).fetchone()["c"]
        if count:
            snapshot_pool_to_block(conn, open_block)  # close it out under the old series
        else:
            with conn:  # empty open block — just move it into the new series
                conn.execute("UPDATE blocks SET series_id=? WHERE id=?", (new_sid, open_block))
    return new_sid


def create_block(conn):
    with conn:
        cursor = conn.execute(
            f"""INSERT INTO blocks (series_id, start_ranks, created_at_ms)
                VALUES (?, ?, {_now_expr()})""",
            (current_series_id(conn), json.dumps(tracked_ranks(conn))))
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


def add_clip(conn, owner_type, owner_id, label, kind, file_name=None, url=None):
    """owner_type: 'session' | 'block_game'. kind: 'upload' (file_name set,
    stored on disk by the caller — db.py has no filesystem knowledge) |
    'link' (url set)."""
    with conn:
        cursor = conn.execute(
            f"""INSERT INTO clips (owner_type, owner_id, label, kind, file_name, url, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, {_now_expr()})""",
            (owner_type, owner_id, label, kind, file_name, url))
    return cursor.lastrowid


def list_clips(conn, owner_type, owner_id):
    return conn.execute(
        "SELECT * FROM clips WHERE owner_type=? AND owner_id=? ORDER BY created_at_ms",
        (owner_type, owner_id)).fetchall()


def get_clip(conn, clip_id):
    return conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()


def delete_clip(conn, clip_id):
    with conn:
        cursor = conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    return cursor.rowcount > 0


def delete_clips_for_owner(conn, owner_type, owner_id):
    """Delete all clips for one session/block_game (its own record is being
    deleted by the caller). Returns the file_names of any 'upload' clips so
    the caller can unlink them from disk — db.py never touches the
    filesystem beyond sqlite itself."""
    rows = conn.execute(
        "SELECT file_name FROM clips WHERE owner_type=? AND owner_id=? AND kind='upload'",
        (owner_type, owner_id)).fetchall()
    with conn:
        conn.execute("DELETE FROM clips WHERE owner_type=? AND owner_id=?",
                     (owner_type, owner_id))
    return [r["file_name"] for r in rows if r["file_name"]]


def delete_clips_for_block(conn, block_id):
    """Delete all clips attached to any game in a block (the block and its
    block_games rows are being deleted by the caller). Returns file_names of
    any 'upload' clips to unlink."""
    rows = conn.execute(
        """SELECT c.file_name FROM clips c
           JOIN block_games bg ON bg.id = c.owner_id
           WHERE c.owner_type='block_game' AND bg.block_id=? AND c.kind='upload'""",
        (block_id,)).fetchall()
    with conn:
        conn.execute(
            """DELETE FROM clips WHERE owner_type='block_game' AND owner_id IN
               (SELECT id FROM block_games WHERE block_id=?)""", (block_id,))
    return [r["file_name"] for r in rows if r["file_name"]]


# ---------- research entries (VOD review of other players' games) ----------

def create_research_entry(conn, player_name, champion, opp_champion, title, notes):
    with conn:
        cursor = conn.execute(
            f"""INSERT INTO research_entries
                (player_name, champion, opp_champion, title, notes, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, {_now_expr()}, {_now_expr()})""",
            (player_name, champion, opp_champion, title, notes))
    return cursor.lastrowid


def list_research_entries(conn):
    """Newest first — matches the sessions/blocks list convention."""
    return conn.execute("SELECT * FROM research_entries ORDER BY created_at_ms DESC").fetchall()


def get_research_entry(conn, entry_id):
    return conn.execute("SELECT * FROM research_entries WHERE id=?", (entry_id,)).fetchone()


def update_research_entry(conn, entry_id, player_name, champion, opp_champion, title, notes):
    with conn:
        cursor = conn.execute(
            f"""UPDATE research_entries SET player_name=?, champion=?, opp_champion=?,
                title=?, notes=?, updated_at_ms={_now_expr()} WHERE id=?""",
            (player_name, champion, opp_champion, title, notes, entry_id))
    return cursor.rowcount > 0


def delete_research_entry(conn, entry_id):
    """Deletes the entry row only — the caller (app.py) fetches/unlinks its
    screenshot files first, same division of responsibility as
    sessions/blocks (db.py never touches the filesystem)."""
    with conn:
        conn.execute("DELETE FROM research_screenshots WHERE entry_id=?", (entry_id,))
        cursor = conn.execute("DELETE FROM research_entries WHERE id=?", (entry_id,))
    return cursor.rowcount > 0


def add_research_screenshot(conn, entry_id, caption, file_name):
    with conn:
        cursor = conn.execute(
            f"""INSERT INTO research_screenshots (entry_id, caption, file_name, created_at_ms)
                VALUES (?, ?, ?, {_now_expr()})""",
            (entry_id, caption, file_name))
    return cursor.lastrowid


def list_research_screenshots(conn, entry_id):
    return conn.execute(
        "SELECT * FROM research_screenshots WHERE entry_id=? ORDER BY id",
        (entry_id,)).fetchall()


def get_research_screenshot(conn, screenshot_id):
    return conn.execute("SELECT * FROM research_screenshots WHERE id=?", (screenshot_id,)).fetchone()


def delete_research_screenshot(conn, screenshot_id):
    with conn:
        cursor = conn.execute("DELETE FROM research_screenshots WHERE id=?", (screenshot_id,))
    return cursor.rowcount > 0
