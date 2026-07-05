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

from . import db, stats
from .config import PROJECT_ROOT, load_config
from .metrics import METRICS

app = FastAPI(title="Coach Potato")

CRAWL_STATE = {"running": False, "message": "idle", "last_result": None, "error": None}

RANGE_PRESETS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90, "180d": 180, "365d": 365}


def get_db_path() -> Path:
    env = os.environ.get("LOL_DB_PATH")
    if env:
        return Path(env)
    return load_config().db_path


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


def stat_filters(request: Request):
    params = dict(request.query_params)
    puuid = params.get("puuid")
    if not puuid:
        raise HTTPException(400, "puuid query param required")
    from_ms, to_ms = parse_time_range(params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    return {
        "puuid": puuid,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "champion": params.get("champion") or None,
        "queues": queues,
        "rank_tier": params.get("rank_tier") or None,
        "min_games": int(params.get("min_games", 1)),
    }


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
    filters = stat_filters(request)
    conn = get_conn()
    try:
        return stats.matchups(conn, **filters)
    finally:
        conn.close()


@app.get("/api/stats/matchups_by_rank")
def api_matchups_by_rank(request: Request):
    filters = stat_filters(request)
    conn = get_conn()
    try:
        return stats.matchups_by_rank(conn, **filters)
    finally:
        conn.close()


@app.get("/api/stats/summary")
def api_summary(request: Request):
    filters = stat_filters(request)
    conn = get_conn()
    try:
        return stats.summary(conn, **filters)
    finally:
        conn.close()


@app.get("/api/filters")
def api_filters(puuid: str):
    conn = get_conn()
    try:
        return stats.filter_options(conn, puuid)
    finally:
        conn.close()


@app.get("/api/sessions")
def api_sessions():
    conn = get_conn()
    try:
        return [dict(r) for r in db.list_sessions(conn)]
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
        puuids = [r["puuid"] for r in
                  conn.execute("SELECT puuid FROM players WHERE is_tracked=1")]
        sessions = [dict(r) for r in db.list_sessions(conn)]
        return stats.progress_segments(
            conn, puuids, sessions,
            champion=params.get("champion") or None, queues=queues)
    finally:
        conn.close()


@app.get("/api/stats/games")
def api_games(request: Request, from_ms: int | None = None, to_ms: int | None = None):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        players = conn.execute(
            "SELECT puuid, game_name FROM players WHERE is_tracked=1").fetchall()
        names = {r["puuid"]: r["game_name"] for r in players}
        games = stats.games_in_range(
            conn, list(names), from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues)
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
        result = stats.segment_metrics(
            conn, _tracked_puuids(conn), from_ms=from_ms, to_ms=to_ms,
            champion=params.get("champion") or None, queues=queues)
        result["meta"] = METRICS
        return result
    finally:
        conn.close()


@app.get("/api/stats/trends")
def api_trends(request: Request, bucket: str = "month"):
    params = dict(request.query_params)
    queues = [int(q) for q in request.query_params.getlist("queue")] or None
    conn = get_conn()
    try:
        try:
            buckets = stats.trend_buckets(
                conn, _tracked_puuids(conn), bucket=bucket,
                champion=params.get("champion") or None, queues=queues)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"buckets": buckets, "meta": METRICS}
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
    conn = get_conn()
    try:
        db.set_pool(conn, (body.get("main_blind") or "").strip() or None,
                    [str(c).strip() for c in core if str(c).strip()],
                    [str(c).strip() for c in counter if str(c).strip()])
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


@app.get("/api/blocks")
def api_blocks():
    conn = get_conn()
    try:
        names = {r["puuid"]: r["game_name"] for r in
                 conn.execute("SELECT puuid, game_name FROM players WHERE is_tracked=1")}
        games_by_block = {}
        for game in stats.block_games_detailed(conn):
            game["account"] = names.get(game["puuid"], "?")
            games_by_block.setdefault(game["block_id"], []).append(game)
        blocks = []
        for row in db.list_blocks(conn):
            games = games_by_block.get(row["id"], [])
            record = {**dict(row), "games": games,
                      "complete": len(games) >= db.BLOCK_SIZE}
            snapshot = record.pop("pool_snapshot", None)
            record["pool"] = json.loads(snapshot) if snapshot else None
            blocks.append(record)
        return {"blocks": blocks, "block_size": db.BLOCK_SIZE}
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
        config = load_config()
        from .crawler import Crawler
        from .riot_client import RiotClient

        client = RiotClient(config.riot_api_key, platform=config.platform)
        conn = db.connect(config.db_path)

        def status_cb(msg):
            CRAWL_STATE["message"] = msg

        crawler = Crawler(client, conn, status_cb=status_cb)
        results = []
        for game_name, tag_line in config.accounts:
            CRAWL_STATE["message"] = f"crawling {game_name}#{tag_line}"
            results.append(crawler.crawl_player(game_name, tag_line))
        CRAWL_STATE["message"] = "fetching opponent ranks"
        crawler.enrich_ranks()
        crawler.refresh_tracked_ranks()
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
    CRAWL_STATE.update({"running": True, "message": "starting", "error": None})
    threading.Thread(target=_run_crawl, daemon=True).start()
    return {"started": True}


@app.get("/api/crawl/status")
def api_crawl_status():
    return CRAWL_STATE


app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
