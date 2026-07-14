"""FastAPI app: JSON API over the sqlite db + static frontend."""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, db, stats
from .config import PROJECT_ROOT
from .metrics import METRICS
from .riot_client import PLATFORM_ROUTING

app = FastAPI(title="Coach Potato")

CRAWL_STATE = {"running": False, "message": "idle", "last_result": None, "error": None,
               "rate_limited": False}


def _champion_ids():
    """Valid DDragon champion ids from the static roster file (patched by
    re-running the DDragon fetch; see CLAUDE.md)."""
    try:
        data = json.loads((PROJECT_ROOT / "static" / "champions.json").read_text())
        return {c["id"] for c in data["champions"]}
    except (OSError, KeyError, ValueError):
        return set()  # roster file missing/corrupt: skip validation rather than break


CHAMPION_IDS = _champion_ids()

RANGE_PRESETS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "180d": 180, "365d": 365}


def get_db_path() -> Path:
    return config.default_db_path()


def get_conn():
    return db.connect(get_db_path())


def parse_time_range(params: dict, now_ms: int | None = None):
    """Return (from_ms, to_ms) from either range=7d|14d|... or from/to ISO dates."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    preset = params.get("range")
    if preset and preset != "all":
        if preset not in RANGE_PRESETS:
            raise HTTPException(400, f"unknown range {preset!r}")
        return (now_ms - RANGE_PRESETS[preset] * 86_400_000, None)
    from_ms = to_ms = None
    if params.get("from"):
        dt = datetime.strptime(params["from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        from_ms = int(dt.timestamp() * 1000)
    if params.get("to"):
        dt = datetime.strptime(params["to"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_ms = int(dt.timestamp() * 1000) + 86_400_000 - 1  # inclusive end of day
    return (from_ms, to_ms)


def stat_filters(request: Request, conn):
    """Common stat query params. `puuid` may repeat (multi-account) or be
    absent (= all tracked accounts)."""
    params = dict(request.query_params)
    from_ms, to_ms = parse_time_range(params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    return {
        "puuid": request.query_params.getlist("puuid") or _tracked_puuids(conn),
        "from_ms": from_ms,
        "to_ms": to_ms,
        "champion": params.get("champion") or None,
        "queues": queues,
        "rank_tier": params.get("rank_tier") or None,
        "min_games": int(params.get("min_games", 1)),
    }


@app.get("/api/version")
def api_version():
    return {"version": config.app_version(), "repo": config.GITHUB_REPO}


HIDEABLE_VIEWS = {"overview", "matchups", "progress", "trends", "blocks"}


def _hidden_views(conn):
    raw = db.get_settings(conn).get("hidden_views")
    return json.loads(raw) if raw else []


DEFAULT_AUTO_CRAWL_HOURS = 3


def _extra_settings(conn):
    stored = db.get_settings(conn)
    hours = stored.get("auto_crawl_hours")
    last = stored.get("last_crawl_ms")
    return {
        "hidden_views": _hidden_views(conn),
        "auto_crawl_hours": int(hours) if hours is not None else DEFAULT_AUTO_CRAWL_HOURS,
        "last_crawl_ms": int(last) if last else None,
        "hide_my_rank": stored.get("hide_my_rank") == "1",
    }


def _hide_my_rank(conn):
    return db.get_settings(conn).get("hide_my_rank") == "1"


# Own-rank data is nulled at the API boundary when "Hide my rank / LP" is on,
# so every view — including future ones — hides it without its own logic.
# Snapshots keep being recorded; turning the setting off restores everything.
_MY_RANK_KEYS = {"solo_tier", "solo_division", "solo_lp", "start_ranks", "end_ranks"}


def _scrub_my_ranks(value):
    if isinstance(value, dict):
        return {k: (None if k in _MY_RANK_KEYS else _scrub_my_ranks(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_my_ranks(v) for v in value]
    return value


_MY_RANK_KEY_BYTES = [f'"{k}"'.encode() for k in _MY_RANK_KEYS]


@app.middleware("http")
async def redact_my_rank(request: Request, call_next):
    response = await call_next(request)
    if (not request.url.path.startswith("/api")
            or "application/json" not in response.headers.get("content-type", "")):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    headers = {k: v for k, v in response.headers.items() if k != "content-length"}
    # cheap sniff: only payloads that mention a rank key need the settings
    # lookup (keeps e.g. the 2 s crawl-status poll off the db)
    if any(key in body for key in _MY_RANK_KEY_BYTES):
        conn = get_conn()
        try:
            hidden = _hide_my_rank(conn)
        finally:
            conn.close()
        if hidden:
            return JSONResponse(_scrub_my_ranks(json.loads(body)),
                                status_code=response.status_code, headers=headers)
    return Response(content=body, status_code=response.status_code, headers=headers)


@app.get("/api/settings")
def api_get_settings():
    conn = get_conn()
    try:
        settings = config.resolve_settings(conn)
        settings["platforms"] = sorted(PLATFORM_ROUTING)
        settings.update(_extra_settings(conn))
        return settings
    finally:
        conn.close()


@app.put("/api/settings")
def api_put_settings(body: dict):
    api_key = (body.get("riot_api_key") or "").strip()
    accounts = body.get("accounts") or []
    platform = (body.get("platform") or "euw1").strip().lower()
    if not api_key:
        raise HTTPException(400, "Riot API key is required")
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(400, "add at least one account")
    cleaned = []
    for account in accounts:
        account = str(account).strip()
        name, _, tag = account.partition("#")
        if not name or not tag:
            raise HTTPException(400, f"account {account!r} must be Name#TAG")
        cleaned.append(f"{name.strip()}#{tag.strip()}")
    if platform not in PLATFORM_ROUTING:
        raise HTTPException(400, f"unknown platform {platform!r}")
    hidden_views = body.get("hidden_views", [])
    if not isinstance(hidden_views, list) or not set(hidden_views) <= HIDEABLE_VIEWS:
        raise HTTPException(400, f"hidden_views must be a subset of {sorted(HIDEABLE_VIEWS)}")
    hours = body.get("auto_crawl_hours", DEFAULT_AUTO_CRAWL_HOURS)
    if not isinstance(hours, int) or isinstance(hours, bool) or hours < 0:
        raise HTTPException(400, "auto_crawl_hours must be a non-negative whole number")
    hide_my_rank = body.get("hide_my_rank", False)
    if not isinstance(hide_my_rank, bool):
        raise HTTPException(400, "hide_my_rank must be a boolean")
    conn = get_conn()
    try:
        db.set_settings(conn, {
            "riot_api_key": api_key,
            "accounts": json.dumps(cleaned),
            "platform": platform,
            "hidden_views": json.dumps(hidden_views),
            "auto_crawl_hours": str(hours),
            "hide_my_rank": "1" if hide_my_rank else "0",
        })
        settings = config.resolve_settings(conn)
        settings["platforms"] = sorted(PLATFORM_ROUTING)
        settings.update(_extra_settings(conn))
        return settings
    finally:
        conn.close()


@app.get("/api/players")
def players():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.puuid, p.game_name, p.tag_line, p.solo_tier, p.solo_division,
                      p.solo_lp, p.rank_fetched_at_ms,
                      (SELECT COUNT(*) FROM participants pa WHERE pa.puuid = p.puuid)
                          AS total_matches
               FROM players p WHERE p.is_tracked = 1 ORDER BY p.game_name"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/stats/matchups")
def api_matchups(request: Request):
    conn = get_conn()
    try:
        return stats.matchups(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/stats/matchups_by_rank")
def api_matchups_by_rank(request: Request):
    conn = get_conn()
    try:
        return stats.matchups_by_rank(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/stats/summary")
def api_summary(request: Request):
    conn = get_conn()
    try:
        return stats.summary(conn, **stat_filters(request, conn))
    finally:
        conn.close()


@app.get("/api/filters")
def api_filters(request: Request):
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        return stats.filter_options(conn, puuids)
    finally:
        conn.close()


@app.get("/api/sessions")
def api_sessions():
    conn = get_conn()
    try:
        sessions = []
        for row in db.list_sessions(conn):
            record = dict(row)
            raw = record.pop("start_ranks", None)
            record["start_ranks"] = json.loads(raw) if raw else None
            sessions.append(record)
        return sessions
    finally:
        conn.close()


@app.post("/api/sessions")
def api_add_session(body: dict):
    date_str = (body or {}).get("date", "")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    conn = get_conn()
    try:
        session_id = db.add_session(conn, date_str,
                                    title=(body.get("title") or "").strip(),
                                    notes=body.get("notes") or "")
        return {"id": session_id}
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a session on {date_str} already exists")
    finally:
        conn.close()


@app.patch("/api/sessions/{session_id}")
def api_update_session(session_id: int, body: dict):
    title = body.get("title")
    notes = body.get("notes")
    if title is None and notes is None:
        raise HTTPException(400, "provide title and/or notes")
    conn = get_conn()
    try:
        if not db.update_session(conn, session_id, title=title, notes=notes):
            raise HTTPException(404, "no such session")
        return {"updated": True}
    finally:
        conn.close()


@app.get("/api/sessions/export.md")
def api_export_sessions():
    conn = get_conn()
    try:
        rows = db.list_sessions(conn)
    finally:
        conn.close()
    parts = ["# Coaching sessions\n"]
    for row in reversed(rows):  # newest first
        title = row["title"] or "Session"
        parts.append(f"\n## {row['session_date']} — {title}\n")
        if row["notes"]:
            parts.append(f"\n{row['notes']}\n")
    return Response(
        content="".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="coaching-sessions.md"'},
    )


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: int):
    conn = get_conn()
    try:
        if not db.delete_session(conn, session_id):
            raise HTTPException(404, "no such session")
        return {"deleted": True}
    finally:
        conn.close()


@app.get("/api/stats/progress")
def api_progress(request: Request):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        sessions = [dict(r) for r in db.list_sessions(conn)]  # sessions are global
        return stats.progress_segments(
            conn, puuids, sessions,
            champion=params.get("champion") or None, queues=queues)
    finally:
        conn.close()


@app.get("/api/stats/games")
def api_games(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    params = dict(request.query_params)
    if from_ms is None and to_ms is None:
        from_ms, to_ms = parse_time_range(params)  # range=30d / from= / to= also work
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        players = conn.execute(
            "SELECT puuid, game_name FROM players WHERE is_tracked=1").fetchall()
        names = {r["puuid"]: r["game_name"] for r in players}
        puuids = request.query_params.getlist("puuid") or list(names)
        games = stats.games_in_range(
            conn, puuids, from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues,
            opp_champion=params.get("opp_champion") or None,
            rank_tier=params.get("rank_tier") or None)
        for game in games:
            game["account"] = names.get(game["my_puuid"], "?")
        return games
    finally:
        conn.close()


def _tracked_puuids(conn):
    return [r["puuid"] for r in
            conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]


@app.get("/api/stats/metrics")
def api_metrics(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
        result = stats.segment_metrics(
            conn, puuids, from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues)
        result["meta"] = METRICS
        return result
    finally:
        conn.close()


@app.get("/api/stats/games/metrics")
def api_single_game_metrics(match_id: str, puuid: str):
    conn = get_conn()
    try:
        metrics = stats.single_game_metrics(conn, match_id, puuid)
        if metrics is None:
            raise HTTPException(404, "no metrics recorded for that game")
        return {"metrics": metrics, "meta": METRICS}
    finally:
        conn.close()


@app.get("/api/stats/trends")
def api_trends(request: Request, bucket: str = "month"):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        try:
            puuids = request.query_params.getlist("puuid") or _tracked_puuids(conn)
            buckets = stats.trend_buckets(
                conn, puuids, bucket=bucket,
                champion=params.get("champion") or None, queues=queues)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"buckets": buckets, "meta": METRICS}
    finally:
        conn.close()


@app.get("/api/matchups/notes")
def api_matchup_notes():
    conn = get_conn()
    try:
        return db.get_matchup_notes(conn)
    finally:
        conn.close()


@app.put("/api/matchups/notes/{champion}")
def api_put_matchup_note(champion: str, body: dict):
    notes = (body or {}).get("notes")
    if notes is None:
        raise HTTPException(400, "provide notes")
    # match-v5 names differ in case from DDragon ids (FiddleSticks vs
    # Fiddlesticks) — validate case-insensitively, store the name as given
    # because reads key by the match-v5 spelling
    if CHAMPION_IDS and champion.lower() not in {c.lower() for c in CHAMPION_IDS}:
        raise HTTPException(400, f"not a champion: {champion}")
    conn = get_conn()
    try:
        db.set_matchup_note(conn, champion, str(notes))
        return {"saved": True}
    finally:
        conn.close()


@app.get("/api/stats/rank-history")
def api_rank_history():
    conn = get_conn()
    try:
        players = conn.execute(
            """SELECT puuid, game_name, tag_line FROM players
               WHERE is_tracked=1 ORDER BY game_name""").fetchall()
        history = ({} if _hide_my_rank(conn)
                   else stats.rank_history(conn, [p["puuid"] for p in players]))
        return {
            "series": [{"puuid": p["puuid"],
                        "account": f"{p['game_name']}#{p['tag_line']}",
                        "points": history.get(p["puuid"], [])} for p in players],
            "sessions": [{"date": s["session_date"], "title": s["title"]}
                         for s in db.list_sessions(conn)],
        }
    finally:
        conn.close()


@app.get("/api/pool")
def api_get_pool():
    conn = get_conn()
    try:
        return db.get_pool(conn)
    finally:
        conn.close()


@app.put("/api/pool")
def api_put_pool(body: dict):
    core = body.get("core") or []
    counter = body.get("counter") or []
    if not isinstance(core, list) or not isinstance(counter, list):
        raise HTTPException(400, "core and counter must be lists of champion names")
    main_blind = (body.get("main_blind") or "").strip() or None
    core = [str(c).strip() for c in core if str(c).strip()]
    counter = [str(c).strip() for c in counter if str(c).strip()]
    if CHAMPION_IDS:
        unknown = [c for c in [main_blind, *core, *counter]
                   if c and c not in CHAMPION_IDS]
        if unknown:
            raise HTTPException(400, f"not a champion: {', '.join(unknown)}")
    conn = get_conn()
    try:
        db.set_pool(conn, main_blind, core, counter)
        # a block completed before any pool was saved gets this pool stamped
        current = conn.execute(
            """SELECT b.id FROM blocks b WHERE b.pool_snapshot IS NULL
               AND b.id = (SELECT MAX(id) FROM blocks)
               AND (SELECT COUNT(*) FROM block_games WHERE block_id = b.id) >= ?""",
            (db.BLOCK_SIZE,)).fetchone()
        if current:
            db.snapshot_pool_to_block(conn, current["id"])
        return db.get_pool(conn)
    finally:
        conn.close()


def _blocks_payload(conn):
    names = {r["puuid"]: r["game_name"] for r in
             conn.execute("SELECT puuid, game_name FROM players WHERE is_tracked=1")}
    games_by_block = {}
    for game in stats.block_games_detailed(conn):
        game["account"] = names.get(game["puuid"], "?")
        games_by_block.setdefault(game["block_id"], []).append(game)
    blocks = []
    for row in db.list_blocks(conn):
        games = games_by_block.get(row["id"], [])
        closed = row["closed_at_ms"] is not None
        record = {**dict(row), "games": games, "closed": closed,
                  "complete": closed or len(games) >= db.BLOCK_SIZE}
        snapshot = record.pop("pool_snapshot", None)
        record["pool"] = json.loads(snapshot) if snapshot else None
        for key in ("start_ranks", "end_ranks"):
            raw = record.pop(key, None)
            record[key] = json.loads(raw) if raw else None
        blocks.append(record)
    return blocks


@app.get("/api/blocks")
def api_blocks():
    conn = get_conn()
    try:
        return {"blocks": _blocks_payload(conn), "block_size": db.BLOCK_SIZE}
    finally:
        conn.close()


def _game_date(game):
    return datetime.fromtimestamp(game["game_creation_ms"] / 1000,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


def _blocks_for_export(conn, block_id):
    blocks = _blocks_payload(conn)
    if block_id is None:
        return blocks
    selected = [b for b in blocks if b["id"] == block_id]
    if not selected:
        raise HTTPException(404, "no such block")
    return selected


@app.get("/api/blocks/export.md")
def api_blocks_export_md(block_id: int | None = None):
    conn = get_conn()
    try:
        blocks = _blocks_for_export(conn, block_id)
    finally:
        conn.close()
    parts = ["# Block Learnings\n"]
    for block in blocks:
        wins = sum(g["win"] for g in block["games"])
        title = f" — {block['title']}" if block["title"] else ""
        parts.append(f"\n## Block #{block['id']}{title} "
                     f"({wins}–{len(block['games']) - wins})\n")
        pool = block["pool"]
        if pool:
            parts.append(f"\nPool: {pool['main_blind'] or '–'}"
                         f" · Core: {', '.join(pool['core']) or '–'}"
                         f" · Counters: {', '.join(pool['counter']) or '–'}\n")
        parts.append("\n")
        for g in block["games"]:
            opp = f" vs {g['opp_champion']}" if g["opp_champion"] else ""
            line = (f"- {_game_date(g)} · {g['account']} · {g['my_champion']}{opp}"
                    f" · {'W' if g['win'] else 'L'}"
                    f" · {g['kills']}/{g['deaths']}/{g['assists']}")
            note_lines = g["notes"].splitlines() if g["notes"] else []
            if len(note_lines) == 1 and not note_lines[0].startswith("- "):
                line += f" — {note_lines[0]}"
            else:
                # multi-line / list-style notes nest under the game bullet
                for note in note_lines:
                    bullet = note if note.startswith("- ") else f"- {note}"
                    line += f"\n  {bullet}"
            parts.append(line + "\n")
        if block["learnings"]:
            parts.append(f"\n### Learnings\n\n{block['learnings']}\n")
    return Response(
        content="".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="block-learnings.md"'},
    )


@app.get("/api/blocks/export.csv")
def api_blocks_export_csv(block_id: int | None = None):
    import csv
    import io

    conn = get_conn()
    try:
        blocks = _blocks_for_export(conn, block_id)
    finally:
        conn.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["block", "title", "date", "account", "champion", "opponent",
                     "result", "kills", "deaths", "assists", "notes", "learnings"])
    for block in blocks:
        for g in block["games"]:
            writer.writerow([
                block["id"], block["title"], _game_date(g), g["account"],
                g["my_champion"], g["opp_champion"] or "",
                "W" if g["win"] else "L", g["kills"], g["deaths"], g["assists"],
                g["notes"], block["learnings"],
            ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="block-learnings.csv"'},
    )


@app.get("/api/blocks/game-notes")
def api_block_game_notes(opp_champion: str):
    """Read-only: block-game notes from games against the given champion,
    newest first (my champion is filtered client-side)."""
    if not opp_champion:
        raise HTTPException(400, "opp_champion query param required")
    conn = get_conn()
    try:
        titles = {b["id"]: b["title"] for b in db.list_blocks(conn)}
        names = {r["puuid"]: r["game_name"] for r in
                 conn.execute("SELECT puuid, game_name FROM players WHERE is_tracked=1")}
        notes = [{
            "block_id": g["block_id"],
            "block_title": titles.get(g["block_id"], ""),
            "match_id": g["match_id"],
            "puuid": g["puuid"],
            "account": names.get(g["puuid"], "?"),
            "game_creation_ms": g["game_creation_ms"],
            "my_champion": g["my_champion"],
            "opp_champion": g["opp_champion"],
            "win": g["win"],
            "notes": g["notes"],
        } for g in stats.block_games_detailed(conn)
            if g["opp_champion"] == opp_champion and g["notes"].strip()]
        notes.reverse()  # block_games_detailed is oldest first
        return notes
    finally:
        conn.close()


@app.post("/api/blocks/games")
def api_add_block_game(body: dict):
    match_id = (body or {}).get("match_id")
    puuid = (body or {}).get("puuid")
    if not match_id or not puuid:
        raise HTTPException(400, "match_id and puuid required")
    conn = get_conn()
    try:
        known = conn.execute(
            "SELECT 1 FROM participants WHERE match_id=? AND puuid=?",
            (match_id, puuid)).fetchone()
        if not known:
            raise HTTPException(404, "no such game for that account")
        try:
            block_id = db.add_game_to_block(conn, match_id, puuid)
        except sqlite3.IntegrityError:
            holder = db.find_block_for_game(conn, match_id, puuid)
            raise HTTPException(409, f"game is already in Block #{holder}")
        return {"block_id": block_id}
    finally:
        conn.close()


@app.post("/api/blocks/{block_id}/close")
def api_close_block(block_id: int):
    conn = get_conn()
    try:
        exists = conn.execute("SELECT 1 FROM blocks WHERE id=?", (block_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "no such block")
        if not db.close_block(conn, block_id):
            raise HTTPException(409, "block is already closed or complete")
        return {"closed": True}
    finally:
        conn.close()


@app.patch("/api/blocks/{block_id}")
def api_update_block(block_id: int, body: dict):
    title = body.get("title")
    learnings = body.get("learnings")
    if title is None and learnings is None:
        raise HTTPException(400, "provide title and/or learnings")
    conn = get_conn()
    try:
        if not db.update_block(conn, block_id, title=title, learnings=learnings):
            raise HTTPException(404, "no such block")
        return {"updated": True}
    finally:
        conn.close()


@app.patch("/api/blocks/games/{entry_id}")
def api_update_block_game(entry_id: int, body: dict):
    if body.get("notes") is None:
        raise HTTPException(400, "provide notes")
    conn = get_conn()
    try:
        if not db.update_block_game(conn, entry_id, body["notes"]):
            raise HTTPException(404, "no such block game")
        return {"updated": True}
    finally:
        conn.close()


@app.delete("/api/blocks/games/{entry_id}")
def api_delete_block_game(entry_id: int):
    conn = get_conn()
    try:
        if not db.delete_block_game(conn, entry_id):
            raise HTTPException(404, "no such block game")
        return {"deleted": True}
    finally:
        conn.close()


@app.delete("/api/blocks/{block_id}")
def api_delete_block(block_id: int):
    conn = get_conn()
    try:
        if not db.delete_block(conn, block_id):
            raise HTTPException(404, "no such block")
        return {"deleted": True}
    finally:
        conn.close()


def _run_crawl():
    try:
        from .crawler import Crawler
        from .riot_client import RateLimiter, RiotClient

        conn = db.connect(get_db_path())
        settings = config.resolve_settings(conn)
        if not settings["configured"]:
            raise RuntimeError("not configured — set your API key and accounts in Settings")

        def on_wait(seconds):
            if seconds >= 2:  # ignore sub-second burst throttling
                CRAWL_STATE["rate_limited"] = True

        client = RiotClient(settings["riot_api_key"], platform=settings["platform"],
                            limiter=RateLimiter(on_wait=on_wait))

        def status_cb(msg):
            CRAWL_STATE["message"] = msg
            CRAWL_STATE["rate_limited"] = False  # progress resumed

        crawler = Crawler(client, conn, status_cb=status_cb)
        results = []
        for account in settings["accounts"]:
            game_name, _, tag_line = account.partition("#")
            CRAWL_STATE["message"] = f"crawling {account}"
            results.append(crawler.crawl_player(game_name, tag_line))
        CRAWL_STATE["message"] = "fetching opponent ranks"
        crawler.enrich_ranks()
        crawler.backfill_metrics()
        crawler.refresh_tracked_ranks()
        db.set_settings(conn, {"last_crawl_ms": str(int(time.time() * 1000))})
        conn.close()
        CRAWL_STATE["last_result"] = results
        CRAWL_STATE["message"] = "done"
        CRAWL_STATE["error"] = None
    except Exception as exc:  # surfaced via /api/crawl/status
        CRAWL_STATE["error"] = str(exc)
        CRAWL_STATE["message"] = "failed"
    finally:
        CRAWL_STATE["running"] = False


@app.post("/api/crawl")
def api_crawl():
    if CRAWL_STATE["running"]:
        return JSONResponse({"detail": "crawl already running"}, status_code=409)
    CRAWL_STATE.update({"running": True, "message": "starting", "error": None,
                        "rate_limited": False})
    threading.Thread(target=_run_crawl, daemon=True).start()
    return {"started": True}


@app.get("/api/crawl/status")
def api_crawl_status():
    return CRAWL_STATE


app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
