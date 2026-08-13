"""Import local VOD metadata from Ascent's recording database.

Ascent (the clip/recording tool) keeps a sqlite database at
`%LOCALAPPDATA%\\Ascent\\recordings.db` with one row per recorded game. Each row
carries `game_match_id` — the match-v5 id, exactly the key Coach Potato stores
matches under — so a recording can be lined up with a crawled game and its
timeline without any guesswork.

Two rules this module exists to enforce:

1. **Ascent's database is only ever read.** It is a live database owned by
   another running process, usually with a large WAL beside it. Opening it
   directly could trigger a checkpoint or block Ascent, so it is snapshotted to
   a temp file (db + -wal + -shm together, so the WAL's contents are included)
   and the snapshot is what we query.
2. **Video files are never copied, moved or deleted.** Only the path is stored;
   forgetting a recording forgets the row, not the file.

Only recordings whose match is already in our `matches` table are imported —
an unmatched recording has nothing to attach to, and would just be noise.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

# Ascent's own column names, kept in one place so a rename upstream is a
# one-line fix here rather than a hunt.
ASCENT_TABLE = "recordings"
ASCENT_COLUMNS = ("uuid", "video_path", "created_at", "duration_s", "game_match_id")


def default_ascent_db_path():
    """Where Ascent keeps its database on this machine, or None."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path = Path(local) / "Ascent" / "recordings.db"
    return path if path.exists() else None


def _snapshot(source: Path, into: Path):
    """Copy the database and its WAL/SHM siblings so the snapshot sees
    everything Ascent has written, including uncommitted WAL pages."""
    shutil.copy2(source, into)
    for suffix in ("-wal", "-shm"):
        sibling = Path(str(source) + suffix)
        if sibling.exists():
            shutil.copy2(sibling, Path(str(into) + suffix))


def read_ascent_recordings(db_path):
    """-> list of dicts, newest first. Raises FileNotFoundError if absent."""
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"no Ascent database at {source}")
    with tempfile.TemporaryDirectory(prefix="cp-ascent-") as tmp:
        snapshot = Path(tmp) / "recordings.db"
        _snapshot(source, snapshot)
        conn = sqlite3.connect(snapshot)
        conn.row_factory = sqlite3.Row
        try:
            names = {r["name"] for r in conn.execute(
                f"PRAGMA table_info({ASCENT_TABLE})")}
            if not names:
                raise ValueError(f"{source} has no '{ASCENT_TABLE}' table")
            missing = [c for c in ASCENT_COLUMNS if c not in names]
            if missing:
                raise ValueError(
                    f"{source} is missing expected column(s): {', '.join(missing)}")
            rows = conn.execute(
                f"""SELECT {', '.join(ASCENT_COLUMNS)} FROM {ASCENT_TABLE}
                    WHERE game_match_id IS NOT NULL AND game_match_id != ''
                    ORDER BY created_at DESC""").fetchall()
        finally:
            conn.close()
        # materialise before the temp dir goes away
        return [dict(r) for r in rows]


def sync(conn, db_path, known_match_ids=None):
    """Import every Ascent recording whose match we have crawled.

    Returns {"seen", "imported", "skipped_unmatched", "skipped_missing_file"}.
    Idempotent: re-running refreshes paths without touching our own columns.
    """
    rows = read_ascent_recordings(db_path)
    if known_match_ids is None:
        known_match_ids = {r["match_id"] for r in conn.execute(
            "SELECT match_id FROM matches")}
    result = {"seen": len(rows), "imported": 0,
              "skipped_unmatched": 0, "skipped_missing_file": 0}
    from server import db  # local import: db imports nothing from us

    for row in rows:
        match_id = row["game_match_id"]
        if match_id not in known_match_ids:
            result["skipped_unmatched"] += 1
            continue
        video = row["video_path"]
        # a recording whose file has been deleted or moved is not useful, and
        # storing it would put a dead ▶ button on the game
        if not video or not os.path.exists(video):
            result["skipped_missing_file"] += 1
            continue
        db.upsert_recording(conn, {
            "uuid": row["uuid"],
            "match_id": match_id,
            "video_path": video,
            "started_at_ms": int(row["created_at"]),
            "duration_s": row["duration_s"],
        })
        result["imported"] += 1
    return result


# YouTube renders a description's timestamps as clickable chapters only if the
# first one is 0:00, there are at least three, and each is at least 10s long.
# Anything shorter is silently dropped, so the list is built to satisfy them.
YT_MIN_CHAPTERS = 3
YT_MIN_CHAPTER_S = 10


def fmt_timestamp(ms):
    """M:SS, or H:MM:SS past an hour — the formats YouTube parses."""
    total = max(0, int(ms // 1000))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


# How each stored event type reads in a chapter list. Kills and deaths get a
# running count ("Kill 3") because "which one was that" is the usual question;
# towers and objectives carry their own label in `detail`.
_EVENT_LABELS = {"kill": "Kill", "death": "Death", "assist": "Assist"}
# When several events land in the same chapter slot, the most notable wins.
# This is a personal review VOD, so YOUR deaths and kills outrank the map
# objectives that happened around them — a death swallowed by a voidgrub
# chapter is exactly the moment you wanted to jump to.
_EVENT_PRIORITY = {"death": 5, "kill": 4, "objective": 3, "inhibitor": 2,
                   "tower": 1, "assist": 0}


def build_chapters(marks, duration_s=None):
    """Timeline events -> [(video_ms, label)], always opening at 0:00.

    Events closer together than YouTube's 10s minimum are folded into one
    chapter — a kill and the tower that follows it are one moment on the video —
    keeping the most notable label of the group. Anything past the end of the
    video is dropped.
    """
    limit = int(duration_s * 1000) if duration_s else None
    counts = {}
    entries = []
    for mark in sorted(marks, key=lambda m: m["video_ms"]):
        ms = int(mark["video_ms"])
        if limit is not None and ms >= limit:
            continue
        kind = mark.get("event_type", "death")
        detail = (mark.get("detail") or "").strip()
        if detail:
            # a leading "-" marks an event against the player's team
            label = f"Lost {detail[1:].lower()}" if detail.startswith("-") else detail
        else:
            counts[kind] = counts.get(kind, 0) + 1
            label = f"{_EVENT_LABELS.get(kind, kind.title())} {counts[kind]}"
        entries.append((ms, label, _EVENT_PRIORITY.get(kind, 0)))

    # built as (ms, label, priority) so a merge can pick the better label
    chapters = [(0, "Game start", 99)]
    for ms, label, priority in entries:
        if ms - chapters[-1][0] >= YT_MIN_CHAPTER_S * 1000:
            chapters.append((ms, label, priority))
            continue
        # too close for YouTube to accept: fold into the previous chapter,
        # upgrading its label if this event is the more notable one. The 0:00
        # opener has priority 99 so it is never displaced.
        if priority > chapters[-1][2]:
            chapters[-1] = (chapters[-1][0], label, priority)
    return [(ms, label) for ms, label, _ in chapters]


def build_description(conn, recording, puuid, extra=""):
    """A ready-to-paste YouTube description: what the game was, then a chapter
    per death so the video is navigable without scrubbing."""
    row = conn.execute(
        """SELECT m.game_creation_ms, m.game_duration_s, m.queue_id,
                  me.champion_name, me.win, me.kills, me.deaths, me.assists,
                  me.team_position, opp.champion_name AS opp_champion
           FROM matches m
           JOIN participants me ON me.match_id = m.match_id AND me.puuid = ?
           LEFT JOIN participants opp ON opp.match_id = m.match_id
               AND opp.team_id != me.team_id AND opp.team_position = me.team_position
           WHERE m.match_id = ?""",
        (puuid, recording["match_id"])).fetchone()

    lines = []
    if row:
        from datetime import datetime, timezone
        played = datetime.fromtimestamp(
            row["game_creation_ms"] / 1000, timezone.utc).strftime("%d %b %Y")
        matchup = row["champion_name"]
        if row["opp_champion"]:
            matchup += f" vs {row['opp_champion']}"
        lines.append(f"{matchup} — {played}")
        lines.append(
            f"{'Win' if row['win'] else 'Loss'} · "
            f"{row['kills']}/{row['deaths']}/{row['assists']} · "
            f"{int(row['game_duration_s'] // 60)} min"
            + (f" · {row['team_position'].title()}" if row["team_position"] else ""))
        lines.append("")

    marks = timeline_markers(conn, recording["match_id"], puuid,
                             recording["offset_ms"])
    chapters = build_chapters(marks, recording["duration_s"])
    if len(chapters) >= YT_MIN_CHAPTERS:
        lines.append("Chapters")
    for ms, label in chapters:
        lines.append(f"{fmt_timestamp(ms)} {label}")
    if extra:
        lines.extend(["", extra])
    return "\n".join(lines).strip()


def reveal_in_file_manager(path):
    """Open the OS file manager with this file selected.

    The manual-upload path: the browser can't hand a local file to
    youtube.com, but it can put the file one drag away. Popen, not run() —
    Explorer returns a non-zero exit code even on success, and we don't care
    about its output either way.
    """
    import subprocess
    import sys

    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"video file is gone: {target}")
    # never shell=True — the path comes from a database row, not a literal
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{target}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target.parent)])
    return True


def preferred_source(conn, match_id, puuid):
    """Which event source to read for one game — never a mix of both.

    The timeline and Ascent's log describe the SAME game, so reading both
    double-counts every death. Pick whichever has more events: a timeline
    imported before the event set widened holds deaths only and loses to the
    log's full list, while a recomputed timeline (all event types, plus
    positions) wins. Self-correcting, with no flag to keep in step.
    """
    rows = conn.execute(
        """SELECT source, COUNT(*) AS c FROM player_map_events
           WHERE match_id=? AND puuid=? GROUP BY source
           ORDER BY c DESC, source='timeline' DESC LIMIT 1""",
        (match_id, puuid)).fetchall()
    return rows[0]["source"] if rows else "timeline"


def death_markers(conn, match_id, puuid, offset_ms=0):
    """Death timestamps for one game as video positions.

    Coach Potato already stores each death's `timestamp_ms` (from the match
    timeline) in player_map_events. Ascent starts recording within a few
    seconds of the game itself — measured across this user's library, recording
    duration matches game duration to within ~2-6s — so a timeline timestamp
    maps essentially straight onto a video position. `offset_ms` is the
    per-recording nudge for the cases where it doesn't.
    """
    rows = conn.execute(
        """SELECT timestamp_ms, event_type, detail FROM player_map_events
           WHERE match_id=? AND puuid=? AND event_type='death' AND source=?
           ORDER BY timestamp_ms""",
        (match_id, puuid, preferred_source(conn, match_id, puuid))).fetchall()
    return [{"timestamp_ms": r["timestamp_ms"], "event_type": "death",
             "detail": r["detail"],
             "video_ms": max(0, r["timestamp_ms"] + offset_ms)} for r in rows]


def positioned_markers(conn, match_id, puuid, offset_ms=0):
    """Events that can actually be drawn on the map, from whichever source has
    coordinates — which in practice means Riot's timeline, since Live Client
    Data (the Ascent log) has none.

    Deliberately NOT filtered by `preferred_source`: the chapter list wants the
    source with the most events (often the log), but the map wants the ones with
    positions. Reading them separately means a game can have rich log-derived
    chapters AND its timeline death dots, instead of the map going blank
    whenever the log happens to win. No double-counting risk here — only
    timeline rows ever have x/y.
    """
    rows = conn.execute(
        """SELECT timestamp_ms, event_type, detail, x, y FROM player_map_events
           WHERE match_id=? AND puuid=? AND x IS NOT NULL AND y IS NOT NULL
           ORDER BY timestamp_ms""", (match_id, puuid)).fetchall()
    return [{"timestamp_ms": r["timestamp_ms"], "event_type": r["event_type"],
             "detail": r["detail"], "x": r["x"], "y": r["y"],
             "video_ms": max(0, r["timestamp_ms"] + offset_ms)} for r in rows]


def timeline_markers(conn, match_id, puuid, offset_ms=0, types=None):
    """Every stored timeline event for one game as video positions — kills,
    deaths, assists, towers, inhibitors and objectives. This is what the VOD
    chapter list is built from; `death_markers` stays deaths-only because the
    ☠ seek buttons under the video are specifically about deaths."""
    params = [match_id, puuid, preferred_source(conn, match_id, puuid)]
    filter_sql = ""
    if types:
        filter_sql = f"AND event_type IN ({','.join('?' * len(types))})"
        params.extend(types)
    rows = conn.execute(
        f"""SELECT timestamp_ms, event_type, detail, x, y FROM player_map_events
            WHERE match_id=? AND puuid=? AND source=? {filter_sql}
            ORDER BY timestamp_ms""", params).fetchall()
    return [{"timestamp_ms": r["timestamp_ms"], "event_type": r["event_type"],
             "detail": r["detail"], "x": r["x"], "y": r["y"],
             "video_ms": max(0, r["timestamp_ms"] + offset_ms)} for r in rows]
